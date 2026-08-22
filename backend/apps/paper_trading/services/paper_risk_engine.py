"""
Hard boundaries the AI decision layer (ai_signal_service.py) can never
override: one lot, one concurrent position, no averaging/pyramiding/
martingale/selling, daily loss limit, max drawdown, consecutive-losses
cooldown, feed staleness, and "can I actually afford this" (spec: no
existing helper for that last one -- see module docstring in
paper_trading/models.py's plan notes; shape mirrors
apps.risk.engine._cap_qty_to_single_symbol_exposure's own
(qty_or_0, reason) return convention).

Reuses apps.risk.engine.check_option_contract_liquidity verbatim for
spread/OI/volume gating -- that function takes plain bid/ask/oi/volume
values, not an apps.execution model, so it's safe to call from here
without pulling in any apps.execution/apps.risk state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from common.constants import RiskEventSeverity

from ..models import PaperOrder, PaperPosition, PaperRiskEvent, PaperTrade


@dataclass
class PaperRiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    quantity: int = 0

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)


def record_risk_event(event_type: str, message: str, severity: str = RiskEventSeverity.INFO) -> None:
    PaperRiskEvent.objects.create(event_type=event_type, message=message, severity=severity)


def daily_loss_limit_breached(account) -> bool:
    if account.daily_start_equity is None or account.daily_start_equity <= 0:
        return False
    daily_pnl_pct = (account.current_equity - account.daily_start_equity) / account.daily_start_equity * Decimal(100)
    return daily_pnl_pct <= -Decimal(str(settings.AI_PAPER_DAILY_LOSS_LIMIT_PCT))


def max_drawdown_breached(account) -> bool:
    if account.peak_equity is None or account.peak_equity <= 0:
        return False
    drawdown_pct = (account.peak_equity - account.current_equity) / account.peak_equity * Decimal(100)
    return drawdown_pct >= Decimal(str(settings.AI_PAPER_MAX_DRAWDOWN_PCT))


def consecutive_losses(account, lookback: int = 20) -> int:
    """Count trailing losses (most recent first) until the first win or `lookback` is exhausted."""
    count = 0
    for trade in PaperTrade.objects.filter(position__account=account).order_by("-closed_at")[:lookback]:
        if trade.net_pnl < 0:
            count += 1
        else:
            break
    return count


def consecutive_losses_cooldown_active(account) -> bool:
    return consecutive_losses(account) >= settings.AI_PAPER_MAX_CONSECUTIVE_LOSSES


def cap_to_available_cash(account, reference_price: Decimal, lot_size: int, requested_lots: int) -> tuple[int, str]:
    """Returns (quantity, reason) -- quantity is 0 (with a reason) if even
    one lot can't be afforded from available_cash; never partially caps
    below a full lot (spec: every trade buys exactly one option lot,
    never a fraction of a lot)."""
    if lot_size <= 0:
        return 0, "contract has no valid lot size"
    if reference_price <= 0:
        return 0, "no valid reference price to size against"
    one_lot_cost = reference_price * lot_size
    if account.available_cash < one_lot_cost:
        return 0, f"insufficient capital: one lot costs {one_lot_cost}, available cash is {account.available_cash}"
    return lot_size * requested_lots, ""


def has_open_position(account) -> bool:
    return PaperPosition.objects.filter(account=account, status=PaperPosition.Status.OPEN).exists()


def has_pending_entry_order(account) -> bool:
    return PaperOrder.objects.filter(
        account=account, purpose=PaperOrder.Purpose.ENTRY, pending_entry_marker=1,
    ).exists()


def trades_today_count(account) -> int:
    today = timezone.localdate()
    return PaperTrade.objects.filter(position__account=account, closed_at__date=today).count()


def check_pre_entry(
    account, contract, *, ltp: float | None, bid: float | None, ask: float | None,
    open_interest: int | None, volume: int | None, feed_age_seconds: float | None,
) -> PaperRiskDecision:
    """The one function ai_signal_service must call before any BUY_*_ONE_LOT
    action is allowed to actually reach the execution engine. Every check
    here is a HARD boundary -- none of it is a "soft" score the AI can
    outweigh with confidence."""
    from apps.risk.engine import check_option_contract_liquidity

    if has_open_position(account):
        return PaperRiskDecision(False, ["MAX_CONCURRENT_POSITIONS: a position is already open"])
    if has_pending_entry_order(account):
        return PaperRiskDecision(False, ["a pending entry order already exists"])
    if settings.MAX_TRADES_PER_DAY and trades_today_count(account) >= settings.MAX_TRADES_PER_DAY:
        return PaperRiskDecision(False, [f"MAX_TRADES_PER_DAY ({settings.MAX_TRADES_PER_DAY}) reached"])
    if feed_age_seconds is not None and feed_age_seconds > settings.AI_PAPER_STALE_QUOTE_SECONDS:
        return PaperRiskDecision(False, [f"market data is stale ({feed_age_seconds:.0f}s old)"])
    if daily_loss_limit_breached(account):
        return PaperRiskDecision(False, [f"AI_PAPER_DAILY_LOSS_LIMIT_PCT ({settings.AI_PAPER_DAILY_LOSS_LIMIT_PCT}%) breached"])
    if max_drawdown_breached(account):
        return PaperRiskDecision(False, [f"AI_PAPER_MAX_DRAWDOWN_PCT ({settings.AI_PAPER_MAX_DRAWDOWN_PCT}%) breached"])
    if consecutive_losses_cooldown_active(account):
        return PaperRiskDecision(False, [f"AI_PAPER_MAX_CONSECUTIVE_LOSSES ({settings.AI_PAPER_MAX_CONSECUTIVE_LOSSES}) reached -- cooldown"])

    ok, liquidity_reason = check_option_contract_liquidity(contract, bid, ask, open_interest, volume)
    if not ok:
        return PaperRiskDecision(False, [liquidity_reason])

    reference_price = Decimal(str(ask if ask is not None else ltp))
    qty, cash_reason = cap_to_available_cash(account, reference_price, contract.lot_size, settings.LOTS_PER_TRADE)
    if qty <= 0:
        return PaperRiskDecision(False, [cash_reason])

    return PaperRiskDecision(True, [], qty)


def assert_never_averaging_pyramiding_martingale(account, action: str) -> None:
    """Structural guard, not just a config flag: called by
    execution_engine before it EVER opens a second entry order while a
    position is open, regardless of what settings say. Belt-and-
    suspenders on top of settings.ALLOW_AVERAGING etc. already being
    hard-pinned false at startup (config/settings.py raises if any of
    them are true)."""
    if has_open_position(account):
        raise PermissionError(
            f"Refusing to {action}: a position is already open -- averaging/pyramiding/"
            f"martingale are structurally forbidden regardless of signal quality."
        )
