"""
Live-broker execution (manual Phase 4: "Live broker integration").
Mirrors apps.execution.paper_executor's function signatures and
transaction/equity-update discipline exactly, on purpose -- the
difference between paper and live should be ONLY "does this place a
real order or just simulate one", not two differently-structured
pipelines that could drift apart and behave inconsistently.

SAFETY NOTES (read before ever setting BROKER_MODE=live):
  - This module does NOT re-implement or bypass any risk check. By the
    time open_position_live() is called, apps.risk.engine.check_pre_trade
    has already approved the trade and computed its size -- this module
    only ever executes what was already approved.
  - Every order placed here is INTRADAY (see broker_client.place_order's
    producttype) -- there is no overnight-position code path in this
    scaffold at all, matching a same-day index-derivatives strategy.
  - Fill confirmation is polled (not assumed instant) via
    get_order_status -- see _wait_for_fill. If the poll window times
    out, _wait_for_fill now attempts to CANCEL the order
    (BrokerClient.cancel_order) before giving up, and the resulting
    OrderNotFilledError says explicitly whether that cancel was
    confirmed. This reduces but does NOT eliminate the risk of an
    order filling at the broker after this codebase has already marked
    the signal REJECTED and stopped tracking it -- a genuine gap any
    system built on polling (rather than broker webhooks) has. If a
    cancel-failed error is ever logged, check Angel One's order book
    by hand before assuming no position was opened.
  - This has NOT been tested against a real Angel One account. Treat it
    as a reviewed-but-unverified starting point: read it end-to-end
    yourself, and test with the smallest possible real quantity before
    trusting it with real size.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.market_data.broker_client import BrokerClient, get_broker_client
from apps.risk.models import AccountEquity
from common.constants import PositionSide, SignalStatus, SignalType

from .models import OpenPosition
from .paper_executor import _pnl_for

logger = logging.getLogger(__name__)

FILL_POLL_INTERVAL_SECONDS = 2
FILL_POLL_MAX_ATTEMPTS = 15  # 30 seconds total -- if a market order on
# an index derivative hasn't filled by then, something is wrong enough
# that a human should look at it rather than this function retrying
# indefinitely.


class OrderNotFilledError(Exception):
    """Raised when an order doesn't reach a filled state within the poll window."""


def _wait_for_fill(client: BrokerClient, order_id: str) -> dict:
    for _ in range(FILL_POLL_MAX_ATTEMPTS):
        order = client.get_order_status(order_id)
        status = (order.get("status") or "").lower()
        if status in ("complete", "filled"):
            return order
        if status in ("rejected", "cancelled"):
            raise OrderNotFilledError(f"Order {order_id} was {status}: {order.get('text')}")
        time.sleep(FILL_POLL_INTERVAL_SECONDS)

    # Our poll window ran out, but the order may still be genuinely
    # pending at the broker -- attempt to cancel it before giving up,
    # so it (probably) can't fill later completely outside this
    # codebase's tracking. See BrokerClient.cancel_order's own
    # docstring: this reduces, not eliminates, that risk.
    cancelled = client.cancel_order(order_id)
    if cancelled:
        raise OrderNotFilledError(
            f"Order {order_id} did not fill within the poll window and was cancelled."
        )
    raise OrderNotFilledError(
        f"Order {order_id} did not fill within the poll window AND the cancel attempt "
        f"failed/could not be confirmed -- CHECK THE BROKER'S ORDER BOOK MANUALLY. "
        f"This order may still be live and could fill without this codebase knowing."
    )


def open_position_live(signal) -> OpenPosition:
    """
    Places a real order for an approved signal (BUY to open a LONG,
    SELL to open a SHORT -- e.g. apps.options.index_direction_strategy's
    PE-side case, see paper_executor.open_position_from_signal's
    docstring for why that's a SELL/short on the underlying rather than
    a real option order), waits for confirmation, and only then creates
    the OpenPosition row (using the ACTUAL average fill price, not the
    signal's entry_price estimate -- real fills slip). If the order
    fails or doesn't fill, the signal is marked REJECTED with the
    failure reason rather than EXECUTED -- an order that never filled
    must never silently look like a successful trade in the log.
    """
    client = get_broker_client()
    qty = signal.position_size or 0
    if qty <= 0:
        signal.status = SignalStatus.REJECTED
        signal.reason += " [live execution: position_size was 0, order not placed]"
        signal.save(update_fields=["status", "reason"])
        raise ValueError(f"Cannot place a live order for {signal.symbol} with qty=0.")

    side = PositionSide.LONG if signal.signal_type == SignalType.BUY else PositionSide.SHORT
    order_side = "BUY" if side == PositionSide.LONG else "SELL"

    try:
        order_id = client.place_order(signal.symbol, order_side, qty, order_type="MARKET")
        filled_order = _wait_for_fill(client, order_id)
    except Exception as exc:
        logger.exception("Live order placement/fill failed for signal %s", signal.pk)
        signal.status = SignalStatus.REJECTED
        signal.reason += f" [live execution failed: {exc}]"
        signal.save(update_fields=["status", "reason"])
        from apps.admin_tools.audit import log_action
        log_action(
            action="order_rejected",
            actor_label="live_executor",
            target=signal,
            details={"symbol": signal.symbol, "qty": qty, "error": str(exc), "mode": "live"},
        )
        raise

    fill_price = Decimal(str(filled_order.get("averageprice") or signal.entry_price))
    filled_qty = int(filled_order.get("filledshares") or qty)

    from django.conf import settings

    trailing_distance = None
    peak_price = None
    if getattr(settings, "TRAILING_STOP_ENABLED", False):
        # Same convention as paper_executor.open_position_from_signal --
        # trail by the actual fill's distance to the planned stop, not
        # the signal's estimated entry_price (real fills slip). abs()
        # for the same SHORT-stop-sits-above-entry reason as there.
        trailing_distance = abs(fill_price - signal.stop_loss)
        peak_price = fill_price

    with transaction.atomic():
        position = OpenPosition.objects.create(
            signal=signal,
            symbol=signal.symbol,
            side=side,
            qty=filled_qty,
            entry_price=fill_price,
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
        actor_label="live_executor",
        target=position,
        details={
            "symbol": position.symbol, "qty": filled_qty, "fill_price": str(fill_price),
            "broker_order_id": order_id, "mode": "live",
        },
    )

    logger.info(
        "LIVE position opened: %s x%d @ %s (order_id=%s)",
        signal.symbol, filled_qty, fill_price, order_id,
    )
    return position


def close_position_live(position: OpenPosition, reason: str) -> None:
    """
    Places a real closing order -- SELL to close a LONG, BUY (buy to
    cover) to close a SHORT -- waits for the fill, and applies the
    ACTUAL realized P&L (fill price, not an estimate) to AccountEquity
    -- same discipline as paper_executor.close_position: this is the
    only live-mode code path allowed to write to AccountEquity.
    current_equity. P&L uses paper_executor._pnl_for (not reimplemented
    here) so paper and live can never compute it two different ways.
    """
    client = get_broker_client()
    closing_side = "SELL" if position.side == PositionSide.LONG else "BUY"
    order_id = client.place_order(position.symbol, closing_side, position.qty, order_type="MARKET")
    filled_order = _wait_for_fill(client, order_id)
    exit_price = Decimal(str(filled_order.get("averageprice")))

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
        actor_label="live_executor",
        target=position,
        details={
            "symbol": position.symbol, "exit_price": str(exit_price), "pnl": str(pnl),
            "reason": reason, "broker_order_id": order_id, "mode": "live",
        },
    )

    logger.info(
        "LIVE position closed: %s @ %s, pnl=%s, reason=%s (order_id=%s)",
        position.symbol, exit_price, pnl, reason, order_id,
    )


def check_and_close_positions_live(timeframe: str = "5m") -> list[dict]:
    """
    Same decision logic as apps.execution.paper_executor.check_and_close_positions
    (stop-loss / target / technical exit, in that priority order) but
    calling close_position_live() to actually place the exit order
    instead of simulating it. Deliberately re-imports the shared
    decision helpers from paper_executor / signals.engine rather than
    duplicating the stop/target comparison logic, so "when to exit" stays
    identical between paper and live -- only "how the exit is executed"
    differs.
    """
    from apps.market_data.indicators import compute_indicators
    from apps.signals.engine import should_exit_position

    results = []
    for position in OpenPosition.objects.filter(closed_at__isnull=True):
        ind = compute_indicators(position.symbol, timeframe)
        if ind is None:
            continue
        current_price = Decimal(str(ind["close"]))

        from .trailing_stop import update_trailing_stop
        update_trailing_stop(position, current_price)

        # See paper_executor.check_and_close_positions -- SHORT's
        # stop/target sit on the opposite sides of entry from LONG's.
        if position.side == PositionSide.LONG:
            stop_hit = current_price <= position.stop_loss
            target_hit = position.target_price is not None and current_price >= position.target_price
        else:
            stop_hit = current_price >= position.stop_loss
            target_hit = position.target_price is not None and current_price <= position.target_price

        try:
            if stop_hit:
                close_position_live(position, "Stop-loss hit")
                results.append({"symbol": position.symbol, "closed": True, "reason": "stop_loss"})
                continue

            if target_hit:
                close_position_live(position, "Target hit")
                results.append({"symbol": position.symbol, "closed": True, "reason": "target"})
                continue

            should_exit, exit_reasons = should_exit_position(position.symbol, timeframe, position.side)
            if should_exit:
                close_position_live(position, f"Technical exit: {', '.join(exit_reasons)}")
                results.append({"symbol": position.symbol, "closed": True, "reason": "technical_exit"})
        except Exception:
            # A failed exit order is serious -- log loudly, but keep
            # checking other positions rather than letting one broker
            # error stop risk management for everything else that's open.
            logger.exception("Failed to close live position %s (%s)", position.pk, position.symbol)
            results.append({"symbol": position.symbol, "closed": False, "error": True})

    return results
