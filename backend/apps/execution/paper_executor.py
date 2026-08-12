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
from common.constants import PositionSide, SignalStatus

from .models import OpenPosition
from apps.risk.models import AccountEquity


def open_position_from_signal(signal) -> OpenPosition:
    """
    signal: an apps.signals.models.TradingSignal with status=APPROVED
    and signal_type=BUY. Marks the signal EXECUTED in the same
    transaction as creating the position, so a signal can never end up
    "approved" with an orphaned position, or vice versa.
    """
    from django.conf import settings

    trailing_distance = None
    peak_price = None
    if getattr(settings, "TRAILING_STOP_ENABLED", False):
        # Trail by the exact distance the position was originally
        # sized against (entry - initial stop) -- see
        # apps.execution.trailing_stop's module docstring.
        trailing_distance = signal.entry_price - signal.stop_loss
        peak_price = signal.entry_price

    with transaction.atomic():
        position = OpenPosition.objects.create(
            signal=signal,
            symbol=signal.symbol,
            side=PositionSide.LONG,  # this scaffold's signal engine only ever produces BUY/long
            qty=signal.position_size or 0,
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
    """
    results = []
    for position in OpenPosition.objects.filter(closed_at__isnull=True):
        ind = compute_indicators(position.symbol, timeframe)
        if ind is None:
            continue
        current_price = Decimal(str(ind["close"]))

        from .trailing_stop import update_trailing_stop
        update_trailing_stop(position, current_price)

        if current_price <= position.stop_loss:
            close_position(position, position.stop_loss, "Stop-loss hit")
            results.append({"symbol": position.symbol, "closed": True, "reason": "stop_loss"})
            continue

        if position.target_price and current_price >= position.target_price:
            close_position(position, position.target_price, "Target hit")
            results.append({"symbol": position.symbol, "closed": True, "reason": "target"})
            continue

        should_exit, exit_reasons = should_exit_position(position.symbol, timeframe)
        if should_exit:
            close_position(position, current_price, f"Technical exit: {', '.join(exit_reasons)}")
            results.append({"symbol": position.symbol, "closed": True, "reason": "technical_exit"})
            continue

        mark_to_market(position, current_price)
        results.append({"symbol": position.symbol, "closed": False})

    return results
