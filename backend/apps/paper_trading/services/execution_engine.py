"""
PaperExecutionService -- the internal matching engine. Every fill is
simulated against a real, currently-observed bid/ask (or a conservative
synthetic spread when only LTP is available), with configurable
slippage and latency -- this NEVER calls out to Angel One (see
services/angel_one_guard.py); "execution" here means writing PaperOrder/
PaperOrderEvent rows and moving PaperAccount cash, nothing else.

Order side is always BUY for an entry (long options only, per
ALLOW_OPTION_SELLING=false) and SELL for an exit (closing a long).
"""

from __future__ import annotations

import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.market_data import market_hours

from ..models import PaperOrder, PaperOrderEvent
from . import market_data_reader, paper_risk_engine

TICK_SIZE = Decimal("0.05")  # NSE option premium tick size
MONEY_QUANTUM = Decimal("0.01")


class OrderRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _round_to_tick(price: Decimal, *, up: bool) -> Decimal:
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    ticks = (price / TICK_SIZE).to_integral_value(rounding=rounding)
    return (ticks * TICK_SIZE).quantize(MONEY_QUANTUM)


def _is_valid_tick(price: Decimal) -> bool:
    remainder = (price / TICK_SIZE) % 1
    return remainder == 0


def _record_event(order: PaperOrder, from_status: str, to_status: str, detail: dict | None = None) -> None:
    PaperOrderEvent.objects.create(order=order, from_status=from_status, to_status=to_status, detail_json=detail or {})


def _transition(order: PaperOrder, to_status: str, detail: dict | None = None) -> None:
    from_status = order.status
    order.status = to_status
    order.decided_at = timezone.now()
    if to_status not in PaperOrder.OPEN_STATUSES:
        order.pending_entry_marker = None
    order.save(update_fields=["status", "decided_at", "pending_entry_marker"])
    _record_event(order, from_status, to_status, detail)


def fill_price_for(quote: market_data_reader.ContractQuote, *, is_buy: bool) -> Decimal | None:
    """Buy fills at the best ask + slippage; sell fills at the best bid -
    slippage. If only LTP is available, a conservative configurable
    synthetic half-spread is applied around it first -- never an
    optimistic fill at bare LTP (spec: "Never fill every order
    optimistically at LTP")."""
    slippage_rate = Decimal(str(settings.AI_PAPER_SLIPPAGE_BPS)) / Decimal(10000)
    if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0:
        reference = Decimal(str(quote.ask if is_buy else quote.bid))
    elif quote.ltp is not None and quote.ltp > 0:
        half_spread_rate = Decimal(str(settings.AI_PAPER_SPREAD_FALLBACK_BPS)) / Decimal(20000)
        ltp = Decimal(str(quote.ltp))
        reference = ltp * (Decimal(1) + half_spread_rate) if is_buy else ltp * (Decimal(1) - half_spread_rate)
    else:
        return None
    multiplier = Decimal(1) + slippage_rate if is_buy else Decimal(1) - slippage_rate
    return _round_to_tick(reference * multiplier, up=is_buy)


def simulate_fill_quantity(requested_qty: int, quote: market_data_reader.ContractQuote, lot_size: int) -> int:
    """Partial-fill simulation, using traded volume as the only available
    depth proxy (this platform doesn't ingest full market-depth/order-
    book data) -- rounds DOWN to a whole lot, since a fractional lot must
    never be held (spec: "Every trade must buy exactly one option lot").
    No volume data at all is treated as an optimistic full fill (documented
    limitation, same honesty convention as this codebase's other data-gap
    notes) rather than an unexplainable rejection."""
    if quote.volume is None or quote.volume <= 0:
        return requested_qty
    available = min(requested_qty, quote.volume)
    lots_available = available // lot_size
    return lots_available * lot_size


def _simulate_latency() -> None:
    if settings.AI_PAPER_EXECUTION_LATENCY_MS:
        time.sleep(settings.AI_PAPER_EXECUTION_LATENCY_MS / 1000)


@transaction.atomic
def submit_entry_order(account, contract, quantity: int, *, order_type: str = "market", limit_price: Decimal | None = None) -> PaperOrder:
    """Opens a new BUY entry order. Raises OrderRejected (never silently
    no-ops) for every disallowed condition, so callers always know
    exactly why an order didn't proceed."""
    from ..models import PaperAccount, PaperLedger

    account = PaperAccount.objects.select_for_update().get(pk=account.pk)
    paper_risk_engine.assert_never_averaging_pyramiding_martingale(account, "open a new entry")
    if paper_risk_engine.has_pending_entry_order(account):
        raise OrderRejected("duplicate-order: a pending entry order already exists")

    market_open, closed_reason = market_hours.is_market_open()
    if not market_open:
        raise OrderRejected(f"market-closed: {closed_reason}")

    if not contract.is_active:
        raise OrderRejected(f"expired-contract: {contract} is not active")

    lot_size = contract.lot_size
    if lot_size <= 0 or quantity % lot_size != 0:
        raise OrderRejected("invalid-lot-size: quantity is not a whole multiple of the contract lot size")

    if order_type == "limit":
        if limit_price is None or limit_price <= 0 or not _is_valid_tick(limit_price):
            raise OrderRejected(f"invalid-tick-size: limit_price must be a positive multiple of {TICK_SIZE}")

    quote = market_data_reader.latest_contract_quote(contract)
    if quote is None or quote.ltp is None:
        raise OrderRejected("no live quote available for this contract")
    if quote.age_seconds > settings.AI_PAPER_STALE_QUOTE_SECONDS:
        raise OrderRejected(f"stale-quote: quote is {quote.age_seconds:.0f}s old")

    order = PaperOrder.objects.create(
        account=account, contract=contract, purpose=PaperOrder.Purpose.ENTRY, side=PaperOrder.Side.BUY,
        order_type=order_type, limit_price=limit_price, quantity=quantity, lot_size_snapshot=lot_size,
        status=PaperOrder.Status.NEW, pending_entry_marker=1,
        fill_model_version=settings.AI_PAPER_FILL_MODEL_VERSION,
    )
    _record_event(order, "", order.status, {"contract_id": contract.pk})
    _transition(order, PaperOrder.Status.VALIDATED)
    _transition(order, PaperOrder.Status.ACCEPTED)
    _transition(order, PaperOrder.Status.OPEN)

    executable_ask = fill_price_for(quote, is_buy=True)
    if executable_ask is None:
        _transition(order, PaperOrder.Status.REJECTED, {"reason": "no valid price to fill against"})
        order.reject_reason = "no valid price to fill against"
        order.save(update_fields=["reject_reason"])
        raise OrderRejected("no valid price to fill against")

    if order_type == "limit":
        # A limit buy only fills when the executable ask is AT OR BELOW
        # the limit -- never assumed filled just because a candle's high
        # touched it (spec's own conservative candle-fill rule).
        if executable_ask > limit_price:
            # Left OPEN, not rejected -- a limit order genuinely waits;
            # apps.paper_trading.tasks re-checks OPEN limit orders each
            # cycle via check_pending_limit_orders below.
            return order
        fill_price = min(executable_ask, limit_price)
    else:
        fill_price = executable_ask

    _simulate_latency()

    filled_qty = simulate_fill_quantity(quantity, quote, lot_size)
    if filled_qty < quantity:
        _transition(order, PaperOrder.Status.REJECTED, {
            "reason": "insufficient depth to fill the full lot",
            "requested": quantity, "available": filled_qty,
        })
        order.reject_reason = f"partial-fill: only {filled_qty} of {quantity} available -- refusing a fractional lot"
        order.save(update_fields=["reject_reason"])
        raise OrderRejected(order.reject_reason)

    order_cost = fill_price * filled_qty
    if account.available_cash < order_cost:
        _transition(order, PaperOrder.Status.REJECTED, {"reason": "insufficient capital at fill time"})
        order.reject_reason = f"insufficient-capital: fill costs {order_cost}, available {account.available_cash}"
        order.save(update_fields=["reject_reason"])
        raise OrderRejected(order.reject_reason)

    order.filled_quantity = filled_qty
    order.average_fill_price = fill_price
    order.status = PaperOrder.Status.FILLED
    order.pending_entry_marker = None
    order.save(update_fields=["filled_quantity", "average_fill_price", "status", "pending_entry_marker"])
    _record_event(order, PaperOrder.Status.OPEN, order.status, {"fill_price": str(fill_price), "filled_quantity": filled_qty})

    account.available_cash -= order_cost
    account.used_capital += order_cost
    account.save(update_fields=["available_cash", "used_capital"])
    PaperLedger.objects.create(
        account=account, entry_type=PaperLedger.EntryType.CAPITAL_RESERVE, amount=-order_cost,
        balance_after=account.available_cash, reference_type="paper_order", reference_id=str(order.pk),
    )
    return order


@transaction.atomic
def submit_exit_order(account, position, reason: str) -> PaperOrder:
    """Closes an open position at the best executable bid - slippage.
    Exits always attempt a full fill (never partially simulated) -- this
    platform never leaves risk-reducing exposure dangling waiting for
    more depth."""
    from ..models import PaperAccount, PaperPosition

    account = PaperAccount.objects.select_for_update().get(pk=account.pk)
    position = PaperPosition.objects.select_for_update().get(pk=position.pk)
    if position.status != PaperPosition.Status.OPEN:
        raise OrderRejected("position is not open")

    quote = market_data_reader.latest_contract_quote(position.contract)
    if quote is None or quote.ltp is None:
        raise OrderRejected("no live quote available to exit against")

    fill_price = fill_price_for(quote, is_buy=False)
    if fill_price is None:
        raise OrderRejected("no valid price to exit against")

    order = PaperOrder.objects.create(
        account=account, contract=position.contract, purpose=PaperOrder.Purpose.EXIT, side=PaperOrder.Side.SELL,
        order_type=PaperOrder.OrderType.MARKET, quantity=position.quantity, lot_size_snapshot=position.contract.lot_size,
        status=PaperOrder.Status.NEW, fill_model_version=settings.AI_PAPER_FILL_MODEL_VERSION,
    )
    _record_event(order, "", order.status, {"reason": reason, "quote_age_seconds": quote.age_seconds})
    _transition(order, PaperOrder.Status.VALIDATED)
    _transition(order, PaperOrder.Status.ACCEPTED)
    _transition(order, PaperOrder.Status.OPEN)
    _simulate_latency()

    order.filled_quantity = position.quantity
    order.average_fill_price = fill_price
    order.status = PaperOrder.Status.FILLED
    order.save(update_fields=["filled_quantity", "average_fill_price", "status"])
    _record_event(order, PaperOrder.Status.OPEN, order.status, {"fill_price": str(fill_price)})
    return order


def check_pending_limit_orders() -> None:
    """Re-evaluates every still-OPEN limit entry order against the
    current executable ask -- called once per apps.paper_trading.tasks
    cycle. Expires an order that's been waiting past end-of-day (never
    silently carried into the next session)."""
    from ..models import PaperOrder as _PaperOrder

    market_open, _ = market_hours.is_market_open()
    for order in _PaperOrder.objects.filter(
        status=_PaperOrder.Status.OPEN, order_type=_PaperOrder.OrderType.LIMIT, purpose=_PaperOrder.Purpose.ENTRY,
    ):
        if not market_open:
            _transition(order, _PaperOrder.Status.EXPIRED, {"reason": "market closed before the limit was reached"})
            continue
        quote = market_data_reader.latest_contract_quote(order.contract)
        if quote is None or quote.ltp is None:
            continue
        executable_ask = fill_price_for(quote, is_buy=True)
        if executable_ask is None or executable_ask > order.limit_price:
            continue
        try:
            with transaction.atomic():
                from ..models import PaperAccount, PaperLedger

                account = PaperAccount.objects.select_for_update().get(pk=order.account_id)
                filled_qty = simulate_fill_quantity(order.quantity, quote, order.lot_size_snapshot)
                if filled_qty < order.quantity:
                    continue
                fill_price = min(executable_ask, order.limit_price)
                order_cost = fill_price * filled_qty
                if account.available_cash < order_cost:
                    continue
                order.filled_quantity = filled_qty
                order.average_fill_price = fill_price
                order.status = _PaperOrder.Status.FILLED
                order.pending_entry_marker = None
                order.save(update_fields=["filled_quantity", "average_fill_price", "status", "pending_entry_marker"])
                _record_event(order, _PaperOrder.Status.OPEN, order.status, {"fill_price": str(fill_price)})
                account.available_cash -= order_cost
                account.used_capital += order_cost
                account.save(update_fields=["available_cash", "used_capital"])
                PaperLedger.objects.create(
                    account=account, entry_type=PaperLedger.EntryType.CAPITAL_RESERVE, amount=-order_cost,
                    balance_after=account.available_cash, reference_type="paper_order", reference_id=str(order.pk),
                )
        except Exception:
            continue
