"""
Paper-trading executor (manual Phase 3: "Risk engine, Paper trading,
Logging and audit" -- this is the paper-trading piece). Turns an
APPROVED TradingSignal into a real OpenPosition row, and later closes
it when a stop/target/exit condition fires, updating AccountEquity as
if the trade had really happened.

Paper fills are deliberately not frictionless: cash and option positions use
separate configured per-side slippage/fee assumptions, snapshotted on the
position at entry. Reference prices, simulated fills, gross P&L, costs, and
net realized P&L remain independently auditable.

This is intentionally the ONLY code path that touches AccountEquity's
current_equity / consecutive_losses / peak_equity -- apps.risk.engine
only ever *reads* those fields. Keeping the write path in one place
(here, in paper mode; a future live-broker fill handler, in live mode)
means there's exactly one place that has to get the P&L arithmetic
right.

NOTE: this module is deliberately unconditional (it does not check
settings.BROKER_MODE itself) -- apps.execution.tasks is what decides
whether to call this vs. a real broker order-placement path, so the
mode switch lives in exactly one place (the task), not scattered across
every function that touches an order.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.market_data.indicators import compute_indicators
from apps.signals.engine import should_exit_position
from common.constants import PositionSide, SignalStatus, SignalType

from .models import OpenPosition
from .paper_costs import (
    adverse_fill_price,
    assumptions_from_position,
    cap_quantity_to_reference_stop_risk,
    calculate_paper_settlement,
    configured_assumptions,
)
from apps.risk.models import AccountEquity


def open_position_from_signal(
    signal, *, timeframe: str = "5m", strategy_key: str = "",
    enforce_execution_risk: bool = False,
) -> OpenPosition:
    """
    signal: an apps.signals.models.TradingSignal with status=APPROVED
    and signal_type BUY or SELL. Marks the signal EXECUTED in the same
    transaction as creating the position, so a signal can never end up
    "approved" with an orphaned position, or vice versa.

    side is derived from signal_type (BUY -> LONG, SELL -> SHORT) --
    EXCEPT when signal.option_contract is set (apps.options.
    index_direction_strategy's real-option-order path): buying a CE or a
    PE is always a LONG bet on THAT OPTION's own premium regardless of
    which side it is, so side is forced to LONG there, and the position's
    symbol/entry/stop/target come from the option contract's own
    tradingsymbol/premium (already what signal carries in that case, see
    TradingSignal.option_contract's docstring), not signal.symbol
    (the underlying's name).

    Raises ValueError (and marks the signal REJECTED instead of
    EXECUTED) if position_size rounds down to 0 -- reachable in
    practice, not just in theory: apps.signals.engine.generate_signal
    truncates risk_decision.position_size (already >= 1, guaranteed by
    apps.risk.engine.check_pre_trade) by a regime multiplier as low as
    0.5 and an ML confidence multiplier as low as 0.7, so a qty=1 base
    size in a sideways/high-volatility regime with a low-confidence
    model score truncates to 0. apps.execution.live_executor.
    open_position_live already guards this same case (see its own qty
    check); paper mode must reject the same way instead of silently
    opening a phantom qty=0 position that would then block this symbol
    from trading again (apps.risk.engine._check_exposure allows only
    one open position per symbol) until someone notices and closes it
    by hand.
    """
    from django.conf import settings

    requested_qty = int(signal.position_size or 0)
    if requested_qty <= 0:
        signal.status = SignalStatus.REJECTED
        signal.reason += " [paper execution: position_size was 0, no position opened]"
        signal.save(update_fields=["status", "reason"])
        raise ValueError(f"Cannot open a paper position for {signal.symbol} with qty=0.")

    is_option_order = signal.option_contract_id is not None
    side = PositionSide.LONG if is_option_order or signal.signal_type == SignalType.BUY else PositionSide.SHORT
    position_symbol = signal.option_contract.tradingsymbol if is_option_order else signal.symbol
    cost_assumptions = configured_assumptions(is_option=is_option_order)
    entry_reference_price = Decimal(str(signal.entry_price))
    entry_fill_price = adverse_fill_price(
        entry_reference_price,
        side=side,
        is_entry=True,
        assumptions=cost_assumptions,
    )
    lot_size = int(signal.option_contract.lot_size) if is_option_order else 1
    qty = requested_qty

    trailing_distance = None
    peak_price = None
    if getattr(settings, "TRAILING_STOP_ENABLED", False):
        # Trail by the exact distance the position was originally
        # sized against -- abs() since a SHORT's stop sits ABOVE entry
        # (signal.entry_price - signal.stop_loss would be negative
        # there), see apps.execution.trailing_stop's module docstring.
        trailing_distance = abs(entry_fill_price - signal.stop_loss)
        peak_price = entry_fill_price

    from apps.risk.engine import exposure_check_for_execution

    from .models import ExecutionModeSetting

    rejection_error: str | None = None
    with transaction.atomic():
        mode_row, _ = ExecutionModeSetting.objects.select_for_update().get_or_create(pk=1)
        if mode_row.mode != ExecutionModeSetting.Mode.PAPER:
            raise ValueError("Paper execution is disabled while the effective mode is live.")
        signal = type(signal).objects.select_for_update().get(pk=signal.pk)
        if signal.execution_mode != ExecutionModeSetting.Mode.PAPER:
            raise ValueError(
                f"Signal {signal.pk} was approved for {signal.execution_mode}, not paper execution."
            )
        if signal.status != SignalStatus.APPROVED:
            raise ValueError(f"Signal {signal.pk} is not executable (status={signal.status}).")
        if enforce_execution_risk:
            from apps.risk.engine import validate_signal_for_execution

            risk_ok, risk_reason = validate_signal_for_execution(signal)
            if not risk_ok:
                signal.status = SignalStatus.REJECTED
                signal.reason += f" [execution-time risk veto: {risk_reason}]"
                signal.save(update_fields=["status", "reason"])
                rejection_error = risk_reason
        if rejection_error is None:
            exposure_ok, exposure_reason = exposure_check_for_execution(signal.symbol)
            if not exposure_ok:
                signal.status = SignalStatus.REJECTED
                signal.reason += f" [execution-time risk veto: {exposure_reason}]"
                signal.save(update_fields=["status", "reason"])
                rejection_error = exposure_reason
        if rejection_error is None:
            equity = AccountEquity.objects.select_for_update().get(pk=1)
            max_risk_pct = Decimal(str(settings.RISK_HARD_LIMITS["MAX_RISK_PER_TRADE_PCT"]))
            risk_budget = equity.current_equity * max_risk_pct / Decimal("100")
            qty = cap_quantity_to_reference_stop_risk(
                entry_reference_price=entry_reference_price,
                stop_reference_price=Decimal(str(signal.stop_loss)),
                requested_qty=requested_qty,
                side=side,
                assumptions=cost_assumptions,
                entry_fill_price=entry_fill_price,
                lot_size=lot_size,
                risk_budget=risk_budget,
            )
            if qty <= 0:
                signal.status = SignalStatus.REJECTED
                signal.reason += " [paper execution: modeled costs exhausted the hard risk budget]"
                signal.save(update_fields=["status", "reason"])
                rejection_error = "Modeled paper costs exhaust the per-trade risk budget."
        if rejection_error is None:
            opening_liquidation = calculate_paper_settlement(
                entry_reference_price=entry_reference_price,
                exit_reference_price=entry_reference_price,
                qty=qty,
                side=side,
                assumptions=cost_assumptions,
                entry_fill_price=entry_fill_price,
            )
            signal.status = SignalStatus.EXECUTING
            signal_update_fields = ["status"]
            if qty < requested_qty:
                signal.reason += (
                    f" [paper cost-aware size cap: {requested_qty} -> {qty}]"
                )
                signal_update_fields.append("reason")
            signal.save(update_fields=signal_update_fields)
            position = OpenPosition.objects.create(
                signal=signal,
                option_contract=signal.option_contract if is_option_order else None,
                symbol=position_symbol,
                execution_mode="paper",
                timeframe=timeframe,
                strategy_key=strategy_key,
                side=side,
                # Already an int (see the qty=0 guard above) -- signal.position_size
                # itself is a DecimalField, and OpenPosition.qty (PositiveIntegerField)
                # doesn't coerce a raw Decimal on plain assignment when signal was
                # loaded from a queryset (the real run_trading_cycle path, as opposed
                # to this test suite's in-memory .create() objects). Left as a raw
                # Decimal, it later broke the "qty" audit-log JSON write in
                # log_action() below, which -- even though that failure is caught
                # and swallowed there -- still leaves the surrounding DB transaction
                # poisoned for the rest of this request/task (Django marks
                # connection.needs_rollback=True from the failed internal
                # atomic(savepoint=False) save).
                qty=qty,
                entry_price=entry_fill_price,
                entry_reference_price=entry_reference_price,
                stop_loss=signal.stop_loss,
                target_price=signal.target_1,
                trailing_stop_distance=trailing_distance,
                peak_price=peak_price,
                unrealized_pnl=opening_liquidation.net_pnl,
                last_mark_price=entry_reference_price,
                paper_slippage_bps_per_side=cost_assumptions.slippage_bps_per_side,
                paper_fees_bps_per_side=cost_assumptions.fees_bps_per_side,
            )
            signal.status = SignalStatus.EXECUTED
            signal.save(update_fields=["status"])

    # Commit the rejection audit state before surfacing the error to the task.
    # An exception raised inside transaction.atomic() would roll it back.
    if rejection_error is not None:
        raise ValueError(rejection_error)

    from apps.admin_tools.audit import log_action
    log_action(
        action="order_placed",
        actor_label="paper_executor",
        target=position,
        details={
            "symbol": position.symbol, "side": position.side, "qty": position.qty,
            "requested_qty": requested_qty,
            "entry_reference_price": str(position.entry_reference_price),
            "entry_fill_price": str(position.entry_price), "mode": "paper",
            "option_contract_id": signal.option_contract_id,
            "slippage_bps_per_side": str(position.paper_slippage_bps_per_side),
            "fees_bps_per_side": str(position.paper_fees_bps_per_side),
        },
    )
    return position


def _pnl_for(position: OpenPosition, exit_price: Decimal) -> Decimal:
    if position.side == PositionSide.LONG:
        return (exit_price - position.entry_price) * position.qty
    return (position.entry_price - exit_price) * position.qty


def _settlement_for(position: OpenPosition, exit_reference_price: Decimal):
    """Reprice a paper position using only its persisted cost snapshot."""
    entry_reference_price = position.entry_reference_price or position.entry_price
    return calculate_paper_settlement(
        entry_reference_price=entry_reference_price,
        exit_reference_price=Decimal(str(exit_reference_price)),
        qty=int(position.qty),
        side=position.side,
        assumptions=assumptions_from_position(position),
        entry_fill_price=position.entry_price,
    )


def close_position(position: OpenPosition, exit_price: Decimal, reason: str) -> bool:
    """
    Closes the position and applies its NET realized P&L to AccountEquity in one
    transaction. Also updates consecutive_losses (reset to 0 on a win,
    incremented on a loss) and peak_equity (only ever moves up) --
    these are exactly the two numbers apps.risk.engine's
    _check_consecutive_losses / _check_drawdown read on the next
    candidate trade.
    """
    with transaction.atomic():
        position = OpenPosition.objects.select_for_update().get(pk=position.pk)
        if position.closed_at is not None:
            return False
        if position.execution_mode != "paper":
            raise ValueError(f"Refusing to paper-close {position.execution_mode} position {position.pk}.")
        exit_reference_price = Decimal(str(exit_price))
        settlement = _settlement_for(position, exit_reference_price)
        pnl = settlement.net_pnl

        position.unrealized_pnl = pnl
        position.last_mark_price = exit_reference_price
        position.exit_reference_price = exit_reference_price
        position.exit_price = settlement.exit_fill_price
        position.gross_realized_pnl = settlement.gross_pnl
        position.slippage_cost = settlement.slippage_cost
        position.fees = settlement.fees
        position.total_costs = settlement.total_costs
        position.realized_pnl = settlement.net_pnl
        position.closed_at = timezone.now()
        position.save(update_fields=[
            "unrealized_pnl", "last_mark_price", "exit_reference_price", "exit_price",
            "gross_realized_pnl", "slippage_cost", "fees", "total_costs",
            "realized_pnl", "closed_at",
        ])

        equity = AccountEquity.objects.select_for_update().get(pk=1)
        equity.current_equity += pnl
        equity.peak_equity = max(equity.peak_equity, equity.current_equity)
        equity.consecutive_losses = 0 if pnl > 0 else equity.consecutive_losses + 1
        equity.save(update_fields=["current_equity", "peak_equity", "consecutive_losses"])

    from apps.admin_tools.audit import log_action
    log_action(
        action="order_closed",
        actor_label="paper_executor",
        target=position,
        details={
            "symbol": position.symbol,
            "exit_reference_price": str(exit_reference_price),
            "exit_fill_price": str(settlement.exit_fill_price),
            "gross_pnl": str(settlement.gross_pnl),
            "slippage_cost": str(settlement.slippage_cost),
            "fees": str(settlement.fees),
            "total_costs": str(settlement.total_costs),
            "net_realized_pnl": str(settlement.net_pnl),
            "reason": reason,
            "mode": "paper",
        },
    )
    return True


def mark_to_market(position: OpenPosition, current_price: Decimal) -> None:
    """
    Updates conservative net liquidation P&L on a still-open position WITHOUT
    touching AccountEquity -- only close_position() realizes P&L into equity.
    This is what lets the dashboard show a live-moving P&L number
    (manual section 18) between ticks without the drawdown/exposure
    checks reacting to paper gains/losses that could still reverse.
    """
    current_price = Decimal(str(current_price))
    pnl = _settlement_for(position, current_price).net_pnl
    updated = OpenPosition.objects.filter(
        pk=position.pk, closed_at__isnull=True,
    ).update(unrealized_pnl=pnl, last_mark_price=current_price)
    if updated:
        position.unrealized_pnl = pnl
        position.last_mark_price = current_price


def check_and_close_positions(timeframe: str = "5m") -> list[dict]:
    """
    For every open position: check the stop-loss/target first (hard
    price levels -- these always take priority over the softer
    "should_exit_position" indicator-based exit), then fall back to
    the technical exit conditions from apps.signals.engine.

    Option positions (position.option_contract set -- see that field's
    docstring) are priced from a live option-chain quote
    (apps.options.pricing.latest_ltp_for_contract) instead of
    compute_indicators(position.symbol), which has no candle history for
    an option contract's own symbol, and skip the should_exit_position
    technical-exit fallback for the same reason -- stop/target are the
    only exit triggers for these positions. They're also always LONG
    (buying an option is always long its own premium), so only the LONG
    side of the stop/target comparison below ever applies to them.
    """
    results = []
    for position in OpenPosition.objects.filter(
        closed_at__isnull=True, execution_mode="paper", timeframe=timeframe,
    ):
        if position.option_contract_id is not None:
            from apps.options.pricing import latest_ltp_for_contract

            ltp = latest_ltp_for_contract(position.option_contract)
            if ltp is None:
                continue
            current_price = Decimal(str(ltp))
        else:
            ind = compute_indicators(position.symbol, timeframe)
            if ind is None:
                continue
            current_price = Decimal(str(ind["close"]))

        from .trailing_stop import update_trailing_stop
        update_trailing_stop(position, current_price)

        # SHORT's stop sits ABOVE entry (price rising is the adverse
        # move) and target sits BELOW entry -- the opposite of LONG --
        # so both comparisons flip by side. Option positions are always
        # LONG (see docstring above), so they always take this branch.
        if position.side == PositionSide.LONG:
            stop_hit = current_price <= position.stop_loss
            target_hit = position.target_price is not None and current_price >= position.target_price
        else:
            stop_hit = current_price >= position.stop_loss
            target_hit = position.target_price is not None and current_price <= position.target_price

        if stop_hit:
            # This loop observes discrete quotes/candle closes. Once price has
            # crossed the stop, filling at the old stop would erase any gap;
            # use the observed market reference and then apply exit slippage.
            close_position(position, current_price, "Stop-loss hit")
            results.append({"symbol": position.symbol, "closed": True, "reason": "stop_loss"})
            continue

        if target_hit:
            close_position(position, position.target_price, "Target hit")
            results.append({"symbol": position.symbol, "closed": True, "reason": "target"})
            continue

        if position.option_contract_id is None:
            should_exit, exit_reasons = should_exit_position(position.symbol, timeframe, position.side)
            if should_exit:
                close_position(position, current_price, f"Technical exit: {', '.join(exit_reasons)}")
                results.append({"symbol": position.symbol, "closed": True, "reason": "technical_exit"})
                continue

        mark_to_market(position, current_price)
        results.append({"symbol": position.symbol, "closed": False})

    return results


def flatten_all_positions(timeframe: str = "5m") -> list[dict]:
    """Immediately close every paper position after a kill-switch trip.

    A missing fresh quote must not leave a simulated position open during an
    emergency. In that rare case the persisted raw mark is used; if it has
    never been marked, the reference entry is the conservative deterministic
    fallback.
    """
    results = []
    for position in OpenPosition.objects.filter(
        closed_at__isnull=True, execution_mode="paper",
    ):
        current_price = None
        if position.option_contract_id is not None:
            from apps.options.pricing import latest_ltp_for_contract

            ltp = latest_ltp_for_contract(position.option_contract)
            if ltp is not None:
                current_price = Decimal(str(ltp))
        else:
            ind = compute_indicators(position.symbol, timeframe)
            if ind is not None:
                current_price = Decimal(str(ind["close"]))

        if current_price is None:
            # `unrealized_pnl` is now net of a hypothetical exit and cannot be
            # inverted into an unambiguous market price. Persist the last raw
            # mark explicitly; a never-marked position falls back to its
            # reference entry, producing a conservative immediate cost loss.
            current_price = (
                position.last_mark_price
                or position.entry_reference_price
                or position.entry_price
            )
        closed = close_position(position, current_price, "Emergency kill-switch flatten")
        results.append({
            "symbol": position.symbol,
            "closed": closed,
            "reason": "kill_switch" if closed else "already_closed",
        })
    return results
