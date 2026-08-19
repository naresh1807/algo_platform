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
    (and, for an option-contract signal, apps.risk.engine.
    check_option_contract_liquidity) has already approved the trade and
    computed its size -- this module only ever executes what was already
    approved.
  - Option-contract-aware: when signal.option_contract is set (apps.
    options.index_direction_strategy's real-option-order path), this
    places a real NFO order on that EXACT resolved contract (its own
    tradingsymbol/symbol_token, via BrokerClient.place_order's override
    kwargs) instead of the underlying-symbol order path -- see
    open_position_live's own docstring. Before this existed, LIVE mode
    could only ever place an order on the underlying itself ("NIFTY"),
    never a real option contract, regardless of what the signal said.
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
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.market_data.broker_client import BrokerClient, get_broker_client
from apps.risk.engine import validate_signal_for_execution
from apps.risk.models import AccountEquity
from common.constants import PositionSide, SignalStatus, SignalType

from .models import BrokerOrder, ExecutionModeSetting, OpenPosition
from .paper_executor import _pnl_for, mark_to_market

logger = logging.getLogger(__name__)

FILL_POLL_INTERVAL_SECONDS = 2
FILL_POLL_MAX_ATTEMPTS = 15  # 30 seconds total -- if a market order on
# an index derivative hasn't filled by then, something is wrong enough
# that a human should look at it rather than this function retrying
# indefinitely.

PENDING_BROKER_ORDER_STATUSES = (
    BrokerOrder.Status.CREATED,
    BrokerOrder.Status.SUBMITTED,
    BrokerOrder.Status.UNKNOWN,
)


class OrderNotFilledError(Exception):
    """Raised when an order doesn't reach a filled state within the poll window."""


class OrderRejectedError(OrderNotFilledError):
    """The broker explicitly rejected the order."""


class OrderCancelledError(OrderNotFilledError):
    """The broker confirmed that the order is cancelled."""


class OrderStateUnknownError(OrderNotFilledError):
    """The broker may still have a live/filled order; do not resubmit."""


def _broker_order_tag(journal: BrokerOrder) -> str:
    """Compact deterministic tag accepted by broker order forms."""
    return f"ap{journal.idempotency_key.hex[:18]}"


def _validate_live_signal_integrity(signal) -> int:
    """Validate cross-model order invariants at the irreversible boundary."""
    contract = signal.option_contract
    raw_qty = signal.position_size
    try:
        qty = int(raw_qty)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Live order quantity must be a positive whole number.") from exc
    if raw_qty is None or Decimal(str(raw_qty)) != Decimal(qty) or qty <= 0:
        raise ValueError("Live order quantity must be a positive whole number.")
    if signal.signal_type != SignalType.BUY:
        raise ValueError("Live option orders must be explicit BUY signals.")
    if contract.underlying.strip().upper() != signal.symbol.strip().upper():
        raise ValueError("Signal symbol does not match the selected option contract underlying.")
    if not signal.option_side or contract.option_type != signal.option_side:
        raise ValueError("Signal option side does not match the selected option contract.")
    if signal.strike_price is None or contract.strike != signal.strike_price:
        raise ValueError("Signal strike does not match the selected option contract.")
    if not contract.tradingsymbol.strip() or not contract.symbol_token.strip():
        raise ValueError("Selected option contract is missing broker order identifiers.")
    if not contract.is_active:
        raise ValueError("Selected option contract is inactive.")
    if contract.lot_size <= 0 or qty % contract.lot_size:
        raise ValueError("Live order quantity must be a whole contract-lot multiple.")
    if signal.entry_price <= 0 or signal.stop_loss <= 0:
        raise ValueError("Live option entry and stop prices must be positive.")
    if signal.stop_loss >= signal.entry_price:
        raise ValueError("A long option stop must be below its entry price.")
    if signal.target_1 is not None and signal.target_1 <= signal.entry_price:
        raise ValueError("A long option target must be above its entry price.")
    return qty


def _halt_for_unknown_order(journal: BrokerOrder, detail: str) -> None:
    """Trip the kill switch once when broker exposure cannot be proven."""
    from apps.risk.engine import trigger_kill_switch
    from apps.risk.models import KillSwitchState, RiskEvent
    from common.constants import RiskEventSeverity

    message = f"Broker order journal {journal.pk} is unresolved: {detail}"
    event, _ = RiskEvent.objects.get_or_create(
        event_type="broker_order_unknown",
        message=message,
        defaults={"symbol": journal.symbol, "severity": RiskEventSeverity.CRITICAL},
    )
    state, _ = KillSwitchState.objects.get_or_create(pk=1)
    if not state.is_active:
        trigger_kill_switch("broker_order_unknown", event)


def _validated_fill(order: dict, order_id: str) -> tuple[Decimal, int]:
    """Extract a finite positive price/quantity from a completed order."""
    try:
        price = Decimal(str(order.get("averageprice")))
        quantity = int(order.get("filledshares"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OrderStateUnknownError(
            f"Order {order_id} is complete but its fill data is invalid: "
            f"averageprice={order.get('averageprice')!r}, "
            f"filledshares={order.get('filledshares')!r}."
        ) from exc
    if not price.is_finite() or price <= 0 or quantity <= 0:
        raise OrderStateUnknownError(
            f"Order {order_id} is complete but its fill data is not positive: "
            f"averageprice={price}, filledshares={quantity}."
        )
    return price, quantity


def _reported_filled_quantity(order: dict) -> int:
    """Best-effort filled quantity for non-complete broker states."""
    raw = order.get("filledshares")
    if raw in (None, ""):
        return 0
    try:
        quantity = int(raw)
    except (TypeError, ValueError) as exc:
        raise OrderStateUnknownError(
            f"Broker returned an invalid filledshares value: {raw!r}."
        ) from exc
    if quantity < 0:
        raise OrderStateUnknownError(
            f"Broker returned a negative filledshares value: {quantity}."
        )
    return quantity


def _wait_for_fill(client: BrokerClient, order_id: str) -> dict:
    for _ in range(FILL_POLL_MAX_ATTEMPTS):
        order = client.get_order_status(order_id)
        status = (order.get("status") or "").lower()
        if status in ("complete", "filled"):
            _validated_fill(order, order_id)
            return order
        if status == "rejected":
            if _reported_filled_quantity(order):
                _validated_fill(order, order_id)
                return order
            raise OrderRejectedError(f"Order {order_id} was rejected: {order.get('text')}")
        if status == "cancelled":
            if _reported_filled_quantity(order):
                _validated_fill(order, order_id)
                return order
            raise OrderCancelledError(f"Order {order_id} was cancelled: {order.get('text')}")
        time.sleep(FILL_POLL_INTERVAL_SECONDS)

    # Our poll window ran out, but the order may still be genuinely
    # pending at the broker -- attempt to cancel it before giving up,
    # so it (probably) can't fill later completely outside this
    # codebase's tracking. See BrokerClient.cancel_order's own
    # docstring: this reduces, not eliminates, that risk.
    cancelled = client.cancel_order(order_id)
    if cancelled:
        try:
            final_order = client.get_order_status(order_id)
        except Exception as exc:
            raise OrderStateUnknownError(
                f"Order {order_id} cancellation was acknowledged, but final broker state "
                "could not be verified; manual reconciliation is required."
            ) from exc
        final_status = str(final_order.get("status") or "").lower()
        if final_status in {"complete", "filled"}:
            _validated_fill(final_order, order_id)
            return final_order
        final_filled = _reported_filled_quantity(final_order)
        if final_filled:
            _validated_fill(final_order, order_id)
            return final_order
        if final_status == "cancelled":
            raise OrderCancelledError(
                f"Order {order_id} did not fill within the poll window and was cancelled."
            )
        raise OrderStateUnknownError(
            f"Order {order_id} cancellation was acknowledged, but final state was "
            f"{final_status or 'unknown'} with {final_filled} filled; "
            "manual reconciliation is required."
        )
    raise OrderStateUnknownError(
        f"Order {order_id} did not fill within the poll window AND the cancel attempt "
        f"failed/could not be confirmed -- CHECK THE BROKER'S ORDER BOOK MANUALLY. "
        f"This order may still be live and could fill without this codebase knowing."
    )


def _mark_open_failure(signal, journal: BrokerOrder, exc: Exception) -> OpenPosition | None:
    """Persist a safe terminal/unknown state after an opening failure."""
    should_halt = False
    with transaction.atomic():
        journal = BrokerOrder.objects.select_for_update().get(pk=journal.pk)
        if journal.status == BrokerOrder.Status.FILLED and journal.position_id:
            return OpenPosition.objects.get(pk=journal.position_id)
        if journal.status not in PENDING_BROKER_ORDER_STATUSES:
            return None
        if isinstance(exc, OrderRejectedError):
            journal.status = BrokerOrder.Status.REJECTED
        elif isinstance(exc, OrderCancelledError):
            journal.status = BrokerOrder.Status.CANCELLED
        else:
            journal.status = BrokerOrder.Status.UNKNOWN
        journal.error = str(exc)
        journal.save(update_fields=["status", "error", "updated_at"])

        signal = type(signal).objects.select_for_update().get(pk=signal.pk)
        # Only an explicit reject/cancel proves that retrying via a *new*
        # signal is safe. Unknown stays EXECUTING until reconciliation.
        if journal.status in {BrokerOrder.Status.REJECTED, BrokerOrder.Status.CANCELLED}:
            signal.status = SignalStatus.REJECTED
            signal.reason += f" [live execution failed: {exc}]"
        else:
            signal.status = SignalStatus.EXECUTING
            note = " [live order state unknown; reconciliation required]"
            if note not in signal.reason:
                signal.reason += note
            should_halt = True
        signal.save(update_fields=["status", "reason"])
    if should_halt:
        _halt_for_unknown_order(journal, str(exc))
    return None


def _mark_close_failure(journal: BrokerOrder, exc: Exception) -> bool:
    """Record a close failure unless reconciliation already applied its fill."""
    should_halt = False
    with transaction.atomic():
        journal = BrokerOrder.objects.select_for_update().get(pk=journal.pk)
        if journal.status == BrokerOrder.Status.FILLED:
            return True
        if journal.status not in PENDING_BROKER_ORDER_STATUSES:
            return False
        if isinstance(exc, OrderRejectedError):
            journal.status = BrokerOrder.Status.REJECTED
        elif isinstance(exc, OrderCancelledError):
            journal.status = BrokerOrder.Status.CANCELLED
        else:
            journal.status = BrokerOrder.Status.UNKNOWN
            should_halt = True
        journal.error = str(exc)
        journal.save(update_fields=["status", "error", "updated_at"])
    if should_halt:
        _halt_for_unknown_order(journal, str(exc))
    return False


def _finalize_open_fill(
    signal, journal: BrokerOrder, fill_price: Decimal, filled_qty: int,
    *, timeframe: str = "5m", strategy_key: str = "",
) -> OpenPosition:
    """Atomically record a confirmed opening fill and its position."""
    from django.conf import settings

    is_option_order = signal.option_contract_id is not None
    side = PositionSide.LONG if is_option_order or signal.signal_type == SignalType.BUY else PositionSide.SHORT
    position_symbol = signal.option_contract.tradingsymbol if is_option_order else signal.symbol
    trailing_distance = None
    peak_price = None
    if getattr(settings, "TRAILING_STOP_ENABLED", False):
        trailing_distance = abs(fill_price - signal.stop_loss)
        peak_price = fill_price

    conflict_error = ""
    with transaction.atomic():
        journal = BrokerOrder.objects.select_for_update().get(pk=journal.pk)
        if journal.status == BrokerOrder.Status.FILLED and journal.position_id:
            return OpenPosition.objects.get(pk=journal.position_id)
        signal = type(signal).objects.select_for_update().get(pk=signal.pk)
        position, created = OpenPosition.objects.get_or_create(
            signal=signal,
            defaults={
                "option_contract": signal.option_contract if is_option_order else None,
                "symbol": position_symbol,
                "execution_mode": "live",
                "timeframe": timeframe,
                "strategy_key": strategy_key,
                "side": side,
                "qty": filled_qty,
                "entry_price": fill_price,
                "stop_loss": signal.stop_loss,
                "target_price": signal.target_1,
                "trailing_stop_distance": trailing_distance,
                "peak_price": peak_price,
            },
        )
        if not created and position.closed_at is not None:
            conflict_error = (
                f"Signal {signal.pk} already has a closed position; refusing to attach a new fill."
            )
            journal.status = BrokerOrder.Status.UNKNOWN
            journal.error = conflict_error
            journal.save(update_fields=["status", "error", "updated_at"])
        else:
            signal.status = SignalStatus.EXECUTED
            signal.save(update_fields=["status"])
            journal.position = position
            journal.status = BrokerOrder.Status.FILLED
            journal.filled_quantity = filled_qty
            journal.average_fill_price = fill_price
            journal.error = (
                f"Broker terminal state filled {filled_qty}/{journal.quantity}; "
                "position tracks the actual partial quantity."
                if filled_qty != journal.quantity else ""
            )
            journal.save(update_fields=[
                "position", "status", "filled_quantity", "average_fill_price", "error", "updated_at",
            ])
    if conflict_error:
        _halt_for_unknown_order(journal, conflict_error)
        raise OrderStateUnknownError(conflict_error)
    return position


def _finalize_close_fill(
    journal: BrokerOrder, exit_price: Decimal, filled_qty: int,
) -> tuple[OpenPosition, Decimal, bool]:
    """Apply one confirmed closing fill exactly once.

    Both the synchronous order poller and the periodic reconciler can observe
    the same fill. Locking the durable journal first gives them one shared
    idempotency boundary, so the second observer returns the already-applied
    result instead of double-crediting P&L or tripping a false kill switch.
    """
    conflict_error = ""
    with transaction.atomic():
        journal = BrokerOrder.objects.select_for_update().get(pk=journal.pk)
        position = OpenPosition.objects.select_for_update().get(pk=journal.position_id)
        if journal.status == BrokerOrder.Status.FILLED:
            return position, position.unrealized_pnl, False
        if filled_qty != position.qty:
            conflict_error = (
                f"Closing order filled {filled_qty}/{position.qty}; "
                "partial position reconciliation is required."
            )
        elif position.closed_at is not None:
            conflict_error = (
                "A broker close fill exists for a position that was already "
                "closed by a different local transition."
            )

        if conflict_error:
            journal.status = BrokerOrder.Status.UNKNOWN
            journal.error = conflict_error
            journal.save(update_fields=["status", "error", "updated_at"])
            pnl = Decimal("0")
        else:
            pnl = _pnl_for(position, exit_price)
            position.unrealized_pnl = pnl
            position.closed_at = timezone.now()
            position.save(update_fields=["unrealized_pnl", "closed_at"])

            equity = AccountEquity.objects.select_for_update().get(pk=1)
            equity.current_equity += pnl
            equity.peak_equity = max(equity.peak_equity, equity.current_equity)
            equity.consecutive_losses = 0 if pnl > 0 else equity.consecutive_losses + 1
            equity.save(update_fields=["current_equity", "peak_equity", "consecutive_losses"])

            journal.status = BrokerOrder.Status.FILLED
            journal.filled_quantity = filled_qty
            journal.average_fill_price = exit_price
            journal.error = ""
            journal.save(update_fields=[
                "status", "filled_quantity", "average_fill_price", "error", "updated_at",
            ])

    if conflict_error:
        _halt_for_unknown_order(journal, conflict_error)
        raise OrderStateUnknownError(conflict_error)
    return position, pnl, True


def _broker_intraday_net_quantities(rows: list[dict]) -> dict[str, int]:
    """Normalize Angel One position rows to signed INTRADAY quantities."""
    quantities: dict[str, int] = {}
    for row in rows:
        product = str(row.get("producttype") or row.get("product") or "").upper()
        if product and product not in {"INTRADAY", "MIS"}:
            continue
        symbol = str(row.get("tradingsymbol") or row.get("symbolname") or "").strip()
        try:
            net_qty = int(Decimal(str(row.get("netqty") or row.get("netquantity") or 0)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise OrderStateUnknownError("Broker position quantity was invalid.") from exc
        if symbol and net_qty:
            quantities[symbol] = quantities.get(symbol, 0) + net_qty
    return quantities


def _trip_position_mismatch(reason: str) -> None:
    """Trip the kill switch without attempting a potentially reversing exit."""
    from apps.risk.engine import trigger_kill_switch
    from apps.risk.models import KillSwitchState, RiskEvent
    from common.constants import RiskEventSeverity

    state, _ = KillSwitchState.objects.get_or_create(pk=1)
    if not state.is_active:
        event = RiskEvent.objects.create(
            event_type="broker_position_mismatch",
            message=reason,
            severity=RiskEventSeverity.CRITICAL,
        )
        trigger_kill_switch("broker_position_mismatch", event)


def _verify_broker_exposure_before_exit(client: BrokerClient, position: OpenPosition) -> None:
    """Prove an exit reduces, rather than creates or reverses, broker exposure."""
    actual = _broker_intraday_net_quantities(client.get_positions())
    expected_qty = position.qty if position.side == PositionSide.LONG else -position.qty
    actual_qty = actual.get(position.symbol, 0)
    if actual_qty == expected_qty:
        return
    reason = (
        f"Refusing exit for {position.symbol}: local signed quantity is "
        f"{expected_qty}, broker signed quantity is {actual_qty}. Manual "
        "reconciliation is required to avoid reversing the position."
    )
    _trip_position_mismatch(reason)
    raise OrderRejectedError(reason)


def open_position_live(
    signal, *, timeframe: str = "5m", strategy_key: str = "",
) -> OpenPosition:
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

    When signal.option_contract is set (apps.options.
    index_direction_strategy's real-option-order path), this places a
    real NFO order on that EXACT contract -- tradingsymbol/symbol_token
    from the resolved OptionContract, exchange="NFO" -- via
    BrokerClient.place_order's symbol_token/exchange/tradingsymbol
    override kwargs, instead of looking `signal.symbol` (the underlying,
    e.g. "NIFTY") up in the index-only SYMBOL_TOKENS map. Side is forced
    LONG for an option order, mirroring paper_executor.
    open_position_from_signal exactly -- buying a CE or a PE is always a
    LONG bet on that option's own premium.
    """
    # Claim and journal before any network call. A unique signal/purpose
    # constraint makes direct callers idempotent as well as the Celery path.
    # The singleton mode-row lock also serializes exposure checks across
    # workers, eliminating the old APPROVED->EXECUTING crash window.
    rejection_error: str | None = None
    try:
        with transaction.atomic():
            mode_row, _ = ExecutionModeSetting.objects.select_for_update().get_or_create(pk=1)
            if not settings.LIVE_TRADING_ENABLED:
                raise PermissionError("Live trading is disarmed by server configuration.")
            if mode_row.mode != ExecutionModeSetting.Mode.LIVE:
                raise PermissionError("Effective execution mode is not live.")

            signal = type(signal).objects.select_for_update().select_related(
                "option_contract",
            ).get(pk=signal.pk)
            if signal.execution_mode != ExecutionModeSetting.Mode.LIVE:
                raise ValueError(
                    f"Signal {signal.pk} was approved for {signal.execution_mode}, not live execution."
                )
            if signal.status != SignalStatus.APPROVED:
                raise ValueError(f"Signal {signal.pk} is not executable (status={signal.status}).")

            if signal.option_contract_id is None:
                signal.status = SignalStatus.REJECTED
                signal.reason += " [live execution: no resolved tradable contract]"
                signal.save(update_fields=["status", "reason"])
                rejection_error = (
                    f"Live signal {signal.pk} has no resolved tradable contract; "
                    f"cash index {signal.symbol} cannot be ordered."
                )
            else:
                try:
                    qty = _validate_live_signal_integrity(signal)
                except ValueError as exc:
                    signal.status = SignalStatus.REJECTED
                    signal.reason += f" [live execution invariant failed: {exc}]"
                    signal.save(update_fields=["status", "reason"])
                    rejection_error = str(exc)
                else:
                    risk_ok, risk_reason = validate_signal_for_execution(signal)
                    if not risk_ok:
                        signal.status = SignalStatus.REJECTED
                        signal.reason += f" [execution-time risk veto: {risk_reason}]"
                        signal.save(update_fields=["status", "reason"])
                        rejection_error = risk_reason

            if rejection_error is None:
                is_option_order = True
                side = PositionSide.LONG
                order_side = "BUY"
                position_symbol = signal.option_contract.tradingsymbol
                signal.status = SignalStatus.EXECUTING
                signal.save(update_fields=["status"])
                journal = BrokerOrder.objects.create(
                    signal=signal, purpose=BrokerOrder.Purpose.OPEN,
                    symbol=position_symbol, side=order_side, quantity=qty,
                )
    except IntegrityError as exc:
        raise OrderStateUnknownError(
            f"Signal {signal.pk} already has an opening-order journal; refusing duplicate submission."
        ) from exc

    # Raise only after the atomic block commits the REJECTED state. Raising
    # inside it would roll back the very status/reason that explains why no
    # broker request was made.
    if rejection_error is not None:
        raise ValueError(rejection_error)

    client = get_broker_client()
    try:
        order_id = client.place_order(
            position_symbol, order_side, qty, order_type="MARKET",
            symbol_token=signal.option_contract.symbol_token, exchange="NFO",
            tradingsymbol=signal.option_contract.tradingsymbol,
            order_tag=_broker_order_tag(journal),
        )
        with transaction.atomic():
            journal.broker_order_id = order_id
            journal.status = BrokerOrder.Status.SUBMITTED
            journal.save(update_fields=["broker_order_id", "status", "updated_at"])
        filled_order = _wait_for_fill(client, order_id)
        _validate_broker_order_identity(filled_order, journal)
        fill_price, filled_qty = _validated_fill(filled_order, order_id)
        position = _finalize_open_fill(
            signal, journal, fill_price, filled_qty,
            timeframe=timeframe, strategy_key=strategy_key,
        )
    except Exception as exc:
        resolved_position = _mark_open_failure(signal, journal, exc)
        if resolved_position is not None:
            logger.info(
                "Opening fill for signal %s was applied concurrently by reconciliation.",
                signal.pk,
            )
            return resolved_position
        logger.exception("Live order placement/fill failed for signal %s", signal.pk)
        from apps.admin_tools.audit import log_action
        log_action(
            action=(
                "order_state_unknown"
                if isinstance(exc, OrderStateUnknownError)
                else "order_rejected"
            ),
            actor_label="live_executor",
            target=signal,
            details={"symbol": position_symbol, "qty": qty, "error": str(exc), "mode": "live"},
        )
        raise

    from apps.admin_tools.audit import log_action
    log_action(
        action="order_placed",
        actor_label="live_executor",
        target=position,
        details={
            "symbol": position.symbol, "qty": filled_qty, "fill_price": str(fill_price),
            "broker_order_id": order_id, "mode": "live", "option_contract_id": signal.option_contract_id,
        },
    )

    logger.info(
        "LIVE position opened: %s x%d @ %s (order_id=%s)",
        position_symbol, filled_qty, fill_price, order_id,
    )
    return position


def close_position_live(
    position: OpenPosition, reason: str,
    purpose: str = BrokerOrder.Purpose.CLOSE,
) -> None:
    """
    Places a real closing order -- SELL to close a LONG, BUY (buy to
    cover) to close a SHORT -- waits for the fill, and applies the
    ACTUAL realized P&L (fill price, not an estimate) to AccountEquity
    -- same discipline as paper_executor.close_position: this is the
    only live-mode code path allowed to write to AccountEquity.
    current_equity. P&L uses paper_executor._pnl_for (not reimplemented
    here) so paper and live can never compute it two different ways.

    Uses the same symbol_token/exchange/tradingsymbol overrides as
    open_position_live when position.option_contract is set, so the
    closing order targets the exact same NFO contract the opening order
    did -- position.symbol is already that contract's own tradingsymbol
    in that case (see open_position_live), but the token/exchange still
    need to be supplied explicitly since place_order can't look an NFO
    contract up in SYMBOL_TOKENS.
    """
    with transaction.atomic():
        position = OpenPosition.objects.select_for_update().select_related("option_contract").get(pk=position.pk)
        if position.closed_at is not None:
            return
        if position.execution_mode != ExecutionModeSetting.Mode.LIVE:
            raise ValueError(
                f"Refusing to submit a broker exit for {position.execution_mode} position {position.pk}."
            )
        if position.option_contract_id is None:
            raise OrderStateUnknownError(
                f"Live position {position.pk} has no resolved tradable contract; "
                "automatic broker exit is unsafe."
            )
        active = BrokerOrder.objects.filter(
            position=position,
            status__in=[BrokerOrder.Status.CREATED, BrokerOrder.Status.SUBMITTED, BrokerOrder.Status.UNKNOWN],
        ).exists()
        if active:
            raise OrderStateUnknownError(
                f"Position {position.pk} already has an unresolved closing order."
            )
        journal = BrokerOrder.objects.create(
            position=position, purpose=purpose, symbol=position.symbol,
            side="SELL" if position.side == PositionSide.LONG else "BUY",
            quantity=position.qty,
        )

    client = get_broker_client()
    closing_side = journal.side
    try:
        # Angel One does not expose a broker-enforced reduce-only flag for
        # these orders. Re-read actual exposure immediately before submission
        # and refuse a stale local quantity rather than accidentally opening
        # or reversing a position after manual/auto square-off.
        _verify_broker_exposure_before_exit(client, position)
        if position.option_contract_id is not None:
            order_id = client.place_order(
                position.symbol, closing_side, position.qty, order_type="MARKET",
                symbol_token=position.option_contract.symbol_token, exchange="NFO",
                tradingsymbol=position.symbol, order_tag=_broker_order_tag(journal),
                risk_reducing=True,
            )
        else:
            order_id = client.place_order(
                position.symbol, closing_side, position.qty, order_type="MARKET",
                order_tag=_broker_order_tag(journal), risk_reducing=True,
            )
        with transaction.atomic():
            journal.broker_order_id = order_id
            journal.status = BrokerOrder.Status.SUBMITTED
            journal.save(update_fields=["broker_order_id", "status", "updated_at"])
        filled_order = _wait_for_fill(client, order_id)
        _validate_broker_order_identity(filled_order, journal)
        exit_price, filled_qty = _validated_fill(filled_order, order_id)
        if filled_qty != position.qty:
            raise OrderStateUnknownError(
                f"Closing order {order_id} filled {filled_qty}/{position.qty}; partial position reconciliation required."
            )
    except Exception as exc:
        if _mark_close_failure(journal, exc):
            logger.info(
                "Closing fill for position %s was applied concurrently by reconciliation.",
                position.pk,
            )
            return
        raise

    position, pnl, _ = _finalize_close_fill(journal, exit_price, filled_qty)

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

    Option positions (position.option_contract set) are priced from a
    live option-chain quote (apps.options.pricing.latest_ltp_for_contract)
    instead of compute_indicators(position.symbol, timeframe) -- there is
    no candle history for an option contract's own symbol -- and skip the
    should_exit_position technical-exit fallback for the same reason:
    stop/target are the only exit triggers for these positions. Exactly
    mirrors paper_executor.check_and_close_positions's option branch, so
    "when to exit" never differs between paper and live for the same
    position type.
    """
    from apps.market_data.indicators import compute_indicators
    from apps.signals.engine import should_exit_position

    results = []
    for position in OpenPosition.objects.filter(
        closed_at__isnull=True, execution_mode="live", timeframe=timeframe,
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

        # See paper_executor.check_and_close_positions -- SHORT's
        # stop/target sit on the opposite sides of entry from LONG's.
        # Option positions are always LONG, so they always take that
        # branch.
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

            if position.option_contract_id is None:
                should_exit, exit_reasons = should_exit_position(position.symbol, timeframe, position.side)
                if should_exit:
                    close_position_live(position, f"Technical exit: {', '.join(exit_reasons)}")
                    results.append({"symbol": position.symbol, "closed": True, "reason": "technical_exit"})
                    continue

            mark_to_market(position, current_price)
            results.append({"symbol": position.symbol, "closed": False})
        except Exception:
            # A failed exit order is serious -- log loudly, but keep
            # checking other positions rather than letting one broker
            # error stop risk management for everything else that's open.
            logger.exception("Failed to close live position %s (%s)", position.pk, position.symbol)
            results.append({"symbol": position.symbol, "closed": False, "error": True})

    return results


def sync_live_account_equity() -> dict:
    """Synchronize the live risk baseline when no broker exposure exists."""
    if OpenPosition.objects.filter(
        closed_at__isnull=True, execution_mode=ExecutionModeSetting.Mode.LIVE,
    ).exists():
        return {"synced": False, "reason": "live_positions_open"}
    if BrokerOrder.objects.filter(status__in=[
        BrokerOrder.Status.CREATED,
        BrokerOrder.Status.SUBMITTED,
        BrokerOrder.Status.UNKNOWN,
    ]).exists():
        return {"synced": False, "reason": "orders_unresolved"}

    broker_equity = get_broker_client().get_account_equity()
    today = timezone.localdate()
    with transaction.atomic():
        equity, _ = AccountEquity.objects.select_for_update().get_or_create(
            pk=1,
            defaults={
                "current_equity": broker_equity,
                "daily_start_equity": broker_equity,
                "peak_equity": broker_equity,
                "trading_day": today,
                "source_mode": ExecutionModeSetting.Mode.LIVE,
                "last_broker_sync_at": timezone.now(),
            },
        )
        entering_live = equity.source_mode != ExecutionModeSetting.Mode.LIVE
        new_day = equity.trading_day != today
        equity.current_equity = broker_equity
        equity.source_mode = ExecutionModeSetting.Mode.LIVE
        equity.last_broker_sync_at = timezone.now()
        if entering_live:
            equity.daily_start_equity = broker_equity
            equity.peak_equity = broker_equity
            equity.consecutive_losses = 0
            equity.trading_day = today
        elif new_day:
            equity.daily_start_equity = broker_equity
            equity.trading_day = today
            equity.peak_equity = max(equity.peak_equity, broker_equity)
        else:
            equity.peak_equity = max(equity.peak_equity, broker_equity)
        equity.save(update_fields=[
            "current_equity", "source_mode", "last_broker_sync_at",
            "daily_start_equity", "peak_equity", "consecutive_losses",
            "trading_day",
        ])
    return {"synced": True, "equity": str(broker_equity)}


def reconcile_broker_positions() -> tuple[bool, str]:
    """Fail closed when local live quantities differ from the broker."""
    if BrokerOrder.objects.filter(status__in=[
        BrokerOrder.Status.CREATED,
        BrokerOrder.Status.SUBMITTED,
        BrokerOrder.Status.UNKNOWN,
    ]).exists():
        return False, "Broker orders are unresolved; position reconciliation is deferred."

    broker_rows = get_broker_client().get_positions()
    actual = _broker_intraday_net_quantities(broker_rows)

    expected: dict[str, int] = {}
    for position in OpenPosition.objects.filter(
        closed_at__isnull=True, execution_mode=ExecutionModeSetting.Mode.LIVE,
    ):
        signed_qty = position.qty if position.side == PositionSide.LONG else -position.qty
        expected[position.symbol] = expected.get(position.symbol, 0) + signed_qty

    if actual == expected:
        return True, ""

    reason = f"Broker/local position mismatch (local={expected}, broker={actual})."
    _trip_position_mismatch(reason)
    return False, reason


def flatten_all_positions_live() -> list[dict]:
    """Submit market exits for every open live position after a kill trip."""
    results = []
    for position in OpenPosition.objects.filter(
        closed_at__isnull=True, execution_mode="live",
    ):
        try:
            close_position_live(
                position, "Emergency kill-switch flatten",
                purpose=BrokerOrder.Purpose.FLATTEN,
            )
            results.append({"symbol": position.symbol, "closed": True, "reason": "kill_switch"})
        except Exception:
            logger.exception("Emergency flatten failed for live position %s", position.pk)
            results.append({"symbol": position.symbol, "closed": False, "error": True})
    return results


def _validate_broker_order_identity(broker_row: dict, journal: BrokerOrder) -> None:
    """Reject reconciliation rows that do not match the persisted intent."""
    comparisons = {
        "tradingsymbol": journal.symbol,
        "transactiontype": journal.side,
        "producttype": "INTRADAY",
        "exchange": "NFO",
    }
    for field, expected in comparisons.items():
        actual = broker_row.get(field)
        if actual not in (None, "") and str(actual).upper() != str(expected).upper():
            raise OrderStateUnknownError(
                f"Broker order identity mismatch for {field}: expected {expected}, got {actual}."
            )
    raw_quantity = broker_row.get("quantity") or broker_row.get("orderqty")
    if raw_quantity not in (None, ""):
        try:
            broker_quantity = int(Decimal(str(raw_quantity)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise OrderStateUnknownError("Broker order quantity is invalid.") from exc
        if broker_quantity != journal.quantity:
            raise OrderStateUnknownError(
                f"Broker order quantity mismatch: expected {journal.quantity}, got {broker_quantity}."
            )


def _update_pending_journal(journal_id: int, **changes) -> bool:
    """Conditionally update a journal without overwriting a concurrent fill."""
    changes["updated_at"] = timezone.now()
    return bool(BrokerOrder.objects.filter(
        pk=journal_id, status__in=PENDING_BROKER_ORDER_STATUSES,
    ).update(**changes))


def reconcile_broker_orders() -> list[dict]:
    """Reconcile durable ambiguous/submitted orders against Angel One.

    This function is deliberately explicit and idempotent. It never creates a
    second broker order; it only resolves journal rows by broker id/order tag
    and applies a confirmed complete fill once.
    """
    client = get_broker_client()
    results = []
    pending_ids = list(BrokerOrder.objects.filter(
        status__in=PENDING_BROKER_ORDER_STATUSES,
    ).values_list("pk", flat=True))
    if not pending_ids:
        return results
    try:
        order_book = client.get_order_book()
    except Exception as exc:
        logger.exception("Unable to fetch the broker order book for reconciliation")
        for journal_id in pending_ids:
            if _update_pending_journal(
                journal_id, status=BrokerOrder.Status.UNKNOWN, error=str(exc),
            ):
                journal = BrokerOrder.objects.get(pk=journal_id)
                _halt_for_unknown_order(journal, str(exc))
                results.append({"journal_id": journal_id, "status": "unknown", "error": True})
        return results
    by_id = {
        str(row.get("orderid")): row
        for row in order_book
        if row.get("orderid") not in (None, "")
    }
    by_tag = {
        str(row.get("ordertag") or row.get("orderTag")): row
        for row in order_book
        if row.get("ordertag") or row.get("orderTag")
    }
    for journal_id in pending_ids:
        journal = BrokerOrder.objects.select_related(
            "signal__option_contract", "position",
        ).get(pk=journal_id)
        if journal.status not in PENDING_BROKER_ORDER_STATUSES:
            results.append({"journal_id": journal.pk, "status": journal.status})
            continue
        try:
            broker_row = (
                by_id.get(str(journal.broker_order_id))
                if journal.broker_order_id
                else by_tag.get(_broker_order_tag(journal))
            )
            if broker_row is None:
                _update_pending_journal(
                    journal.pk,
                    status=BrokerOrder.Status.UNKNOWN,
                    error="No broker order found for the persisted order tag.",
                )
                journal.refresh_from_db()
                results.append({"journal_id": journal.pk, "status": "unknown"})
                continue
            broker_id = str(broker_row.get("orderid") or journal.broker_order_id or "")
            if broker_id and not journal.broker_order_id:
                _update_pending_journal(journal.pk, broker_order_id=broker_id)
                journal.refresh_from_db()
                if journal.status not in PENDING_BROKER_ORDER_STATUSES:
                    results.append({"journal_id": journal.pk, "status": journal.status})
                    continue
            _validate_broker_order_identity(broker_row, journal)
            status = str(broker_row.get("status") or "").lower()
            filled_so_far = _reported_filled_quantity(broker_row)
            if status not in {"complete", "filled", "rejected", "cancelled"} and filled_so_far:
                if not broker_id or not client.cancel_order(broker_id):
                    raise OrderStateUnknownError(
                        "Partially filled order could not be cancelled; manual reconciliation is required."
                    )
                broker_row = client.get_order_status(broker_id)
                _validate_broker_order_identity(broker_row, journal)
                status = str(broker_row.get("status") or "").lower()
                filled_so_far = _reported_filled_quantity(broker_row)

            if status in {"rejected", "cancelled"} and not filled_so_far:
                terminal_status = (
                    BrokerOrder.Status.REJECTED if status == "rejected"
                    else BrokerOrder.Status.CANCELLED
                )
                changed = _update_pending_journal(
                    journal.pk, status=terminal_status,
                    error=str(broker_row.get("text") or ""),
                )
                if changed and journal.signal_id:
                    type(journal.signal).objects.filter(
                        pk=journal.signal_id, status=SignalStatus.EXECUTING,
                    ).update(status=SignalStatus.REJECTED)
            elif status in {"complete", "filled"} or filled_so_far:
                price, quantity = _validated_fill(broker_row, broker_id or str(journal.idempotency_key))
                if journal.purpose == BrokerOrder.Purpose.OPEN:
                    _finalize_open_fill(journal.signal, journal, price, quantity)
                else:
                    _finalize_close_fill(journal, price, quantity)
            else:
                _update_pending_journal(
                    journal.pk, status=BrokerOrder.Status.SUBMITTED,
                )
            journal.refresh_from_db()
            results.append({"journal_id": journal.pk, "status": journal.status})
        except Exception as exc:
            logger.exception("Broker-order reconciliation failed for journal %s", journal.pk)
            if _update_pending_journal(
                journal.pk, status=BrokerOrder.Status.UNKNOWN, error=str(exc),
            ):
                journal.refresh_from_db()
                _halt_for_unknown_order(journal, str(exc))
                results.append({"journal_id": journal.pk, "status": "unknown", "error": True})
            else:
                journal.refresh_from_db()
                results.append({"journal_id": journal.pk, "status": journal.status})
    return results
