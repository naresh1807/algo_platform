"""
PaperPositionService -- open/mark-to-market/close, running MFE/MAE
peak/trough tracking (updated on every mark, not just at close), and
trailing-stop application. Mirrors apps.execution.trailing_stop's
one-directional ratchet logic, adapted for PaperPosition.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import PaperPosition
from . import execution_engine


def open_position(account, contract, entry_order, *, stop_loss: Decimal, target_price: Decimal | None, trailing_stop_distance: Decimal | None = None, entry_decision_id=None) -> PaperPosition:
    return PaperPosition.objects.create(
        account=account, contract=contract, entry_order=entry_order, entry_decision_id=entry_decision_id,
        quantity=entry_order.filled_quantity, average_entry_price=entry_order.average_fill_price,
        initial_stop_loss=stop_loss, stop_loss=stop_loss,
        target_price=target_price, trailing_stop_distance=trailing_stop_distance,
        peak_favorable_price=entry_order.average_fill_price, trough_adverse_price=entry_order.average_fill_price,
        last_mark_price=entry_order.average_fill_price,
    )


def mark_to_market(position: PaperPosition, mark_price: Decimal) -> None:
    """Conservative executable mark: callers pass the current bid (what
    the position could actually be sold for right now), not the LTP, so
    unrealized_pnl/MFE/MAE never overstate what's actually realizable."""
    position.last_mark_price = mark_price
    position.unrealized_pnl = (mark_price - position.average_entry_price) * position.quantity
    if position.peak_favorable_price is None or mark_price > position.peak_favorable_price:
        position.peak_favorable_price = mark_price
    if position.trough_adverse_price is None or mark_price < position.trough_adverse_price:
        position.trough_adverse_price = mark_price
    position.save(update_fields=["last_mark_price", "unrealized_pnl", "peak_favorable_price", "trough_adverse_price"])


def apply_trailing_stop(position: PaperPosition, mark_price: Decimal) -> Decimal | None:
    """One-directional ratchet -- stop only ever moves UP as price makes
    a new high since entry (long-only). Returns the old stop if it
    moved, else None."""
    if position.trailing_stop_distance is None:
        return None
    new_stop = mark_price - position.trailing_stop_distance
    if new_stop > position.stop_loss:
        old_stop = position.stop_loss
        position.stop_loss = new_stop
        position.save(update_fields=["stop_loss"])
        return old_stop
    return None


def tighten_stop(position: PaperPosition, new_stop: Decimal) -> None:
    """AI may tighten (raise) a stop, never widen it past where it
    currently sits -- the max-hard-risk-boundary rule enforced here,
    not merely trusted from ai_signal_service's caller."""
    if new_stop < position.stop_loss:
        raise PermissionError(
            f"Refusing to widen stop from {position.stop_loss} to {new_stop} -- "
            f"a stop may only ever tighten after entry, never widen."
        )
    position.stop_loss = new_stop
    position.save(update_fields=["stop_loss"])


@transaction.atomic
def close_position(account, position: PaperPosition, reason: str):
    from . import outcome_service, pnl_service
    from ..models import PaperAccount

    # Re-fetch a FRESH, locked copy rather than trusting the caller's
    # `account` object -- it may have been fetched before this position
    # was even opened (execution_engine.submit_entry_order re-fetches
    # and saves its OWN local copy internally, which never propagates
    # back to the caller's Python object). Passing a stale object
    # straight into pnl_service.settle_trade would apply this trade's
    # cash movements on top of PRE-ENTRY balances, silently erasing the
    # entry's own already-committed deduction. Real bug, caught by
    # tests_idempotency_and_ledger.py's reconciliation test.
    account = PaperAccount.objects.select_for_update().get(pk=account.pk)
    position = PaperPosition.objects.select_for_update().get(pk=position.pk)
    if position.status != PaperPosition.Status.OPEN:
        raise ValueError("position is already closed")

    exit_order = execution_engine.submit_exit_order(account, position, reason)

    position.status = PaperPosition.Status.CLOSED
    position.closed_at = timezone.now()
    position.open_marker = None
    position.save(update_fields=["status", "closed_at", "open_marker"])

    trade = pnl_service.settle_trade(account, position, exit_order, reason)
    result = outcome_service.finalize_trade_outcome(trade, position, reason)
    _backfill_entry_decision(position, trade, result)
    return trade


def _backfill_entry_decision(position: PaperPosition, trade, result) -> None:
    """Links the PaperAIDecision that opened this position to its now-
    known outcome -- spec: every decision, taken or not, ends up with a
    realized-or-hypothetical outcome recorded against it, and a taken
    decision must never be duplicated into a second row."""
    if position.entry_decision_id is None:
        return
    from ..models import PaperAIDecision

    PaperAIDecision.objects.filter(decision_id=position.entry_decision_id).update(
        trade=trade, is_hypothetical=False,
        gross_pnl=trade.gross_pnl, net_pnl=trade.net_pnl, mfe=result.mfe, mae=result.mae,
        net_r=result.r_multiple, outcome_classification=result.outcome_classification,
    )
