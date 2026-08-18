"""
Paper-trading executor (manual Phase 3: "Risk engine, Paper trading,
Logging and audit" -- this is the paper-trading piece). Turns an
APPROVED TradingSignal into a real OpenPosition row, and later closes
it when a stop/target/exit condition fires, updating AccountEquity as
if the trade had really happened.

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
from apps.risk.models import AccountEquity


def open_position_from_signal(signal) -> OpenPosition:
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

    qty = int(signal.position_size or 0)
    if qty <= 0:
        signal.status = SignalStatus.REJECTED
        signal.reason += " [paper execution: position_size was 0, no position opened]"
        signal.save(update_fields=["status", "reason"])
        raise ValueError(f"Cannot open a paper position for {signal.symbol} with qty=0.")

    is_option_order = signal.option_contract_id is not None
    side = PositionSide.LONG if is_option_order or signal.signal_type == SignalType.BUY else PositionSide.SHORT
    position_symbol = signal.option_contract.tradingsymbol if is_option_order else signal.symbol

    trailing_distance = None
    peak_price = None
    if getattr(settings, "TRAILING_STOP_ENABLED", False):
        # Trail by the exact distance the position was originally
        # sized against -- abs() since a SHORT's stop sits ABOVE entry
        # (signal.entry_price - signal.stop_loss would be negative
        # there), see apps.execution.trailing_stop's module docstring.
        trailing_distance = abs(signal.entry_price - signal.stop_loss)
        peak_price = signal.entry_price

    with transaction.atomic():
        position = OpenPosition.objects.create(
            signal=signal,
            option_contract=signal.option_contract if is_option_order else None,
            symbol=position_symbol,
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
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            target_price=signal.target_1,
            trailing_stop_distance=trailing_distance,
            peak_price=peak_price,
        )
        signal.status = SignalStatus.EXECUTED
        signal.save(update_fields=["status"])

    from apps.admin_tools.audit import log_action
    log_action(
        action="order_placed",
        actor_label="paper_executor",
        target=position,
        details={
            "symbol": position.symbol, "side": position.side, "qty": position.qty,
            "entry_price": str(position.entry_price), "mode": "paper",
            "option_contract_id": signal.option_contract_id,
        },
    )
    return position


def _pnl_for(position: OpenPosition, exit_price: Decimal) -> Decimal:
    if position.side == PositionSide.LONG:
        return (exit_price - position.entry_price) * position.qty
    return (position.entry_price - exit_price) * position.qty


def close_position(position: OpenPosition, exit_price: Decimal, reason: str) -> None:
    """
    Closes the position and applies its P&L to AccountEquity in one
    transaction. Also updates consecutive_losses (reset to 0 on a win,
    incremented on a loss) and peak_equity (only ever moves up) --
    these are exactly the two numbers apps.risk.engine's
    _check_consecutive_losses / _check_drawdown read on the next
    candidate trade.
    """
    with transaction.atomic():
        pnl = _pnl_for(position, exit_price)

        position.unrealized_pnl = pnl
        position.closed_at = timezone.now()
        position.save(update_fields=["unrealized_pnl", "closed_at"])

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
        details={"symbol": position.symbol, "exit_price": str(exit_price), "pnl": str(pnl), "reason": reason, "mode": "paper"},
    )


def mark_to_market(position: OpenPosition, current_price: Decimal) -> None:
    """
    Updates unrealized_pnl on a still-open position WITHOUT touching
    AccountEquity -- only close_position() realizes P&L into equity.
    This is what lets the dashboard show a live-moving P&L number
    (manual section 18) between ticks without the drawdown/exposure
    checks reacting to paper gains/losses that could still reverse.
    """
    position.unrealized_pnl = _pnl_for(position, current_price)
    position.save(update_fields=["unrealized_pnl"])


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
    for position in OpenPosition.objects.filter(closed_at__isnull=True):
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
            close_position(position, position.stop_loss, "Stop-loss hit")
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
