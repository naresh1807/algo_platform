"""
The risk engine: apps.signals.engine calls check_pre_trade() with a
candidate trade BEFORE any TradingSignal is marked approved/executed.
This module is the only place allowed to read settings.RISK_HARD_LIMITS
and turn them into an approve/reject decision -- per the manual's
"Risk rules override AI output" principle (section 3), nothing in
apps.signals or apps.learning may bypass this.

Design choice: every check here is a small pure-ish function that
returns (ok: bool, reason: str) so check_pre_trade() can run all of
them and report EVERY failing reason at once (a rejected signal's
`reason` field should tell you everything that was wrong, not just the
first check that happened to fail) -- multi-confirmation logic (section
11) applies just as much to explaining a rejection as to triggering
an entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from .models import AccountEquity, KillSwitchState, RiskEvent
from apps.execution.models import OpenPosition
from apps.monitoring.models import FeedHealthCheck

# manual section 16: "Validate feed freshness" -- if the last successful
# feed-health probe (apps.market_data.tasks.ingest_watchlist_candles) is
# older than this, or reported unhealthy, treat the feed as stale and
# refuse to trade. 15 minutes is deliberately more forgiving than the
# 5-minute ingestion schedule so a single missed tick doesn't halt
# trading, but three-in-a-row missed does.
FEED_STALENESS_THRESHOLD_MINUTES = 15


@dataclass
class RiskDecision:
    approved: bool
    risk_score: float  # 0.0 (rejected outright) to 1.0 (fully clean)
    reasons: list[str] = field(default_factory=list)
    position_size: int | None = None

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "OK"


def _log_event(event_type: str, message: str, severity: str, symbol: str = "") -> RiskEvent:
    return RiskEvent.objects.create(
        symbol=symbol, event_type=event_type, message=message, severity=severity,
    )


def get_equity() -> AccountEquity:
    """
    Returns the singleton row, creating a sane default from settings on
    first use so the engine never crashes with "no equity row yet" on a
    fresh install -- but this default is only ever a *starting point*;
    apps.execution must keep it updated once real trades happen.
    """
    equity, created = AccountEquity.objects.get_or_create(
        pk=1,
        defaults={
            "current_equity": settings.STARTING_EQUITY,
            "daily_start_equity": settings.STARTING_EQUITY,
            "peak_equity": settings.STARTING_EQUITY,
            "trading_day": timezone.localdate(),
        },
    )
    if created:
        _log_event(
            "equity_initialized",
            f"No AccountEquity row existed; initialized to STARTING_EQUITY "
            f"({settings.STARTING_EQUITY}). This is a placeholder -- verify "
            f"it matches your real broker account balance.",
            severity="warning",
        )
    return equity


def is_kill_switch_active() -> bool:
    state, _ = KillSwitchState.objects.get_or_create(pk=1)
    return state.is_active


def trigger_kill_switch(reason: str, event: RiskEvent) -> None:
    """
    Flips the kill switch on. Per the manual's safety rule, this is the
    ONLY function in the codebase that may set is_active=True -- nothing
    in apps.signals or apps.learning should ever import KillSwitchState
    directly and write to it. Deactivating (re-arming) deliberately has
    no counterpart function here; that must be a manual management
    command action (see apps/risk/management/commands, not yet written).
    """
    state, _ = KillSwitchState.objects.get_or_create(pk=1)
    state.is_active = True
    state.triggered_by_event = event
    state.activated_at = timezone.now()
    state.save(update_fields=["is_active", "triggered_by_event", "activated_at"])


def _check_kill_switch() -> tuple[bool, str]:
    if is_kill_switch_active():
        return False, "Kill switch is active -- all new entries are blocked."
    return True, ""


def _check_feed_freshness(symbol: str) -> tuple[bool, str]:
    """
    manual section 16, "Validate feed freshness" / section 21 "Never
    trade when feed is stale". Reads the most recent FeedHealthCheck
    row (written by apps.market_data.tasks) rather than pinging the
    broker inline here -- a pre-trade check needs to be cheap and fast,
    not itself make a network call to the broker on every candidate
    trade.
    """
    latest = FeedHealthCheck.objects.order_by("-checked_at").first()

    if latest is None:
        # No health check has ever run -- e.g. a brand-new install, or
        # BROKER_MODE=paper where ingest_watchlist_candles no-ops and
        # therefore never writes a FeedHealthCheck row. Treat this as a
        # WARNING logged but not a hard block, since paper-trading
        # should still be usable for testing the rest of the pipeline
        # without a live feed at all.
        _log_event(
            "feed_health_unknown",
            "No FeedHealthCheck row exists yet -- feed freshness cannot be verified.",
            severity="warning", symbol=symbol,
        )
        return True, ""

    age = timezone.now() - latest.checked_at
    if age > timedelta(minutes=FEED_STALENESS_THRESHOLD_MINUTES):
        _log_event(
            "stale_feed",
            f"Last feed health check was {age.total_seconds() / 60:.0f} minutes ago "
            f"(> {FEED_STALENESS_THRESHOLD_MINUTES}min threshold).",
            severity="critical", symbol=symbol,
        )
        return False, f"Feed data is stale (last checked {age.total_seconds() / 60:.0f} min ago)."

    if not latest.is_healthy:
        _log_event(
            "unhealthy_feed",
            f"Latest feed health check reported unhealthy: {latest.detail}",
            severity="critical", symbol=symbol,
        )
        return False, f"Broker feed reported unhealthy: {latest.detail or 'no detail'}."

    return True, ""


def _check_drawdown(equity: AccountEquity, symbol: str) -> tuple[bool, str]:
    """
    manual section 13, "Drawdown rules": 15% pauses new entries, 20%
    triggers a full flatten-and-halt. The 5%/10% "reduce size" rules are
    handled separately in _compute_position_size (they scale size down
    rather than blocking outright), since those two are graduated
    responses, not hard stops.
    """
    dd = equity.drawdown_pct
    limits = settings.RISK_HARD_LIMITS

    if dd >= limits["DRAWDOWN_FLATTEN_PCT"]:
        event = _log_event(
            "drawdown_breach",
            f"Drawdown {dd:.1f}% >= flatten threshold {limits['DRAWDOWN_FLATTEN_PCT']}%.",
            severity="critical", symbol=symbol,
        )
        trigger_kill_switch("drawdown_flatten", event)
        return False, f"Drawdown {dd:.1f}% breached flatten threshold -- kill switch triggered."

    if dd >= limits["DRAWDOWN_PAUSE_PCT"]:
        _log_event(
            "drawdown_pause",
            f"Drawdown {dd:.1f}% >= pause threshold {limits['DRAWDOWN_PAUSE_PCT']}%.",
            severity="warning", symbol=symbol,
        )
        return False, f"Drawdown {dd:.1f}% >= pause threshold -- new entries paused."

    return True, ""


def _check_daily_loss(equity: AccountEquity, symbol: str) -> tuple[bool, str]:
    limits = settings.RISK_HARD_LIMITS
    daily_pnl = equity.daily_pnl_pct
    if daily_pnl <= -limits["MAX_DAILY_LOSS_PCT"]:
        _log_event(
            "daily_loss_limit",
            f"Daily P&L {daily_pnl:.1f}% <= -{limits['MAX_DAILY_LOSS_PCT']}%.",
            severity="critical", symbol=symbol,
        )
        return False, f"Daily loss limit hit ({daily_pnl:.1f}%) -- no more entries today."
    return True, ""


def _check_consecutive_losses(equity: AccountEquity, symbol: str) -> tuple[bool, str]:
    limits = settings.RISK_HARD_LIMITS
    if equity.consecutive_losses >= limits["MAX_CONSECUTIVE_LOSSES"]:
        _log_event(
            "consecutive_loss_cooldown",
            f"{equity.consecutive_losses} consecutive losses >= limit "
            f"{limits['MAX_CONSECUTIVE_LOSSES']}.",
            severity="warning", symbol=symbol,
        )
        return False, (
            f"{equity.consecutive_losses} consecutive losses -- cooldown in effect "
            f"until a human resets it or a winning trade breaks the streak."
        )
    return True, ""


def exposure_check_for_execution(symbol: str) -> tuple[bool, str]:
    """
    Public wrapper around _check_exposure(), for apps.execution to call
    a second time immediately before actually opening a position --
    see that module's own comment for why. check_pre_trade() only
    validates exposure at SIGNAL-GENERATION time; if two approved BUY
    signals for the same symbol from different generation cycles ever
    land in the same execution batch, nothing previously re-validated
    exposure between "signal approved" and "position opened." This
    reuses the exact same rule so the two checks can never disagree.
    """
    return _check_exposure(symbol)


def _check_exposure(symbol: str) -> tuple[bool, str]:
    """
    manual section 13, "Exposure rules": max open positions and max
    exposure to any single symbol are checked here (count-based, so
    cheap to check before position sizing runs). The correlated/combined
    exposure rule ("max correlated exposure across symbols that move
    together") now lives in _check_combined_open_risk below instead --
    moved there because a correlation-WEIGHTED percentage figure (see
    that function's docstring) needs the candidate trade's actual
    position size to compute a real risk amount, and sizing hasn't
    happened yet at this point in check_pre_trade.
    """
    limits = settings.RISK_HARD_LIMITS
    open_positions = OpenPosition.objects.filter(closed_at__isnull=True)

    if open_positions.count() >= limits["MAX_OPEN_POSITIONS"]:
        return False, (
            f"Already at max open positions ({limits['MAX_OPEN_POSITIONS']})."
        )

    if open_positions.filter(symbol=symbol).exists():
        return False, f"A position in {symbol} is already open (max one at a time per symbol)."

    return True, ""


def _check_combined_open_risk(
    symbol: str, entry_price: Decimal, stop_loss: Decimal, qty: int, equity: AccountEquity,
) -> tuple[bool, str]:
    """
    manual section 13, "Max correlated exposure: 2-3%" -- enforces
    settings.RISK_HARD_LIMITS["MAX_OPEN_RISK_PCT"] as an actual
    correlation-WEIGHTED combined figure, replacing the previous
    "block if ANY open position is in a symbol correlated above
    HIGH_CORRELATION_THRESHOLD" rule. That older rule is a special case
    of this one at weight=1.0/0.0 -- this version instead uses each open
    position's actual |correlation| with `symbol` as a continuous
    weight (0.0 = fully uncorrelated, contributes nothing; 1.0 = moves
    in lockstep, contributes its full risk), so a moderately-correlated
    position now contributes partial risk instead of either being
    ignored (below the old 0.7 cutoff) or fully blocking a trade (at or
    above it) -- the two-symbol default watchlist (NIFTY/BANKNIFTY,
    themselves highly correlated) is exactly the case where that
    all-or-nothing cutoff was too blunt.

    "Risk amount" here is each position's own price-based risk (qty x
    |entry - stop|), the same quantity apps.risk.engine already sizes
    new trades against -- not notional exposure (that's what
    _check_single_symbol_exposure already covers separately). Returns
    True (does not block) if the correlation matrix can't be computed
    yet (insufficient historical data) -- same "unknown is not a hard
    block" stance the rest of this module uses for missing data.
    """
    from apps.market_data.correlation import compute_correlation_matrix

    limits = settings.RISK_HARD_LIMITS
    open_positions = OpenPosition.objects.filter(closed_at__isnull=True)

    new_risk_amount = float(qty) * float(abs(entry_price - stop_loss))
    combined_risk_amount = new_risk_amount

    try:
        matrix = compute_correlation_matrix()
    except Exception:
        matrix = None

    weighted_detail = []
    if matrix is not None and not matrix.empty and symbol in matrix.columns:
        for position in open_positions:
            if position.symbol == symbol:
                weight = 1.0  # shouldn't normally happen (one-position-per-symbol), but fully count if it does
            elif position.symbol in matrix.columns:
                corr = matrix.at[symbol, position.symbol]
                weight = abs(float(corr)) if corr == corr else 0.0  # corr==corr is a NaN guard
            else:
                weight = 0.0  # no correlation data for this open symbol -- don't assume correlated

            if weight <= 0.0:
                continue
            position_risk_amount = float(position.qty) * float(abs(position.entry_price - position.stop_loss))
            contribution = weight * position_risk_amount
            combined_risk_amount += contribution
            if weight >= 0.3:  # only mention meaningfully-correlated contributors in the reason text
                weighted_detail.append(f"{position.symbol} (weight {weight:.2f})")
    # else: no usable correlation data yet -- combined_risk_amount stays
    # at just the new trade's own risk, i.e. this check can't add a
    # rejection beyond what MAX_RISK_PER_TRADE_PCT already covers.

    combined_risk_pct = (
        combined_risk_amount / float(equity.current_equity) * 100 if equity.current_equity else 0.0
    )

    if combined_risk_pct > limits["MAX_OPEN_RISK_PCT"]:
        detail = f" Correlated contributors: {', '.join(weighted_detail)}." if weighted_detail else ""
        return False, (
            f"Correlation-weighted combined open risk would be {combined_risk_pct:.1f}% of equity, "
            f"over the {limits['MAX_OPEN_RISK_PCT']}% limit.{detail}"
        )
    return True, ""


def _is_options_expiry_day(symbol: str) -> bool:
    """
    manual section 12: "Expiry-day size reduction for options". Checks
    apps.options.OptionContract for any contract on this underlying
    expiring today -- lazy-imported (same pattern as
    _check_combined_open_risk's import of apps.market_data.correlation)
    to avoid apps.risk depending on apps.options at module load time.
    Returns False (not an error) if apps.options has no data yet --
    "unknown whether today is expiry" should not itself block trading;
    it just means this particular size reduction doesn't apply.
    """
    from apps.options.models import OptionContract

    try:
        return OptionContract.objects.filter(
            underlying=symbol, expiry=timezone.localdate(),
        ).exists()
    except Exception:
        return False


def _compute_position_size(
    equity: AccountEquity, entry_price: Decimal, stop_loss: Decimal, symbol: str,
) -> tuple[int, str]:
    """
    manual section 13, "Position sizing":
        Risk amount   = Equity x Risk %
        Stop distance = |entry - stop|  (ATR-derived stop is computed
                                          upstream in apps.signals.engine)
        Position size = Risk amount / Stop distance

    Risk % is scaled down as drawdown increases (the "5% DD: reduce
    size, 10% DD: reduce aggressively" graduated rules) rather than a
    flat percentage always -- capital preservation takes priority over
    consistent sizing once things are going badly. On top of the
    drawdown-based scaling, an options-expiry-day reduction (manual
    section 12) is applied multiplicatively -- both rules can stack
    (e.g. in drawdown AND on expiry day at once), rather than one
    overriding the other.
    """
    limits = settings.RISK_HARD_LIMITS
    base_risk_pct = limits["MAX_RISK_PER_TRADE_PCT"]
    dd = equity.drawdown_pct
    notes = []

    if dd >= 10:
        risk_pct = base_risk_pct * 0.25  # "reduce aggressively"
        notes.append(f"position size reduced aggressively (drawdown {dd:.1f}% >= 10%)")
    elif dd >= 5:
        risk_pct = base_risk_pct * 0.5  # "reduce size"
        notes.append(f"position size reduced (drawdown {dd:.1f}% >= 5%)")
    else:
        risk_pct = base_risk_pct

    if _is_options_expiry_day(symbol):
        risk_pct *= 0.5
        notes.append("position size halved -- today is options expiry day for this underlying")

    risk_amount = equity.current_equity * (Decimal(risk_pct) / Decimal(100))
    stop_distance = abs(entry_price - stop_loss)

    if stop_distance <= 0:
        return 0, "Stop distance is zero or negative -- cannot size the position."

    qty = int(risk_amount / stop_distance)
    return max(qty, 0), "; ".join(notes)


def _check_single_symbol_exposure(symbol: str, entry_price: Decimal, qty: int, equity: AccountEquity) -> tuple[bool, str]:
    limits = settings.RISK_HARD_LIMITS
    notional = entry_price * qty
    exposure_pct = float(notional / equity.current_equity * 100) if equity.current_equity else 0.0
    if exposure_pct > limits["MAX_ONE_SYMBOL_EXPOSURE_PCT"]:
        return False, (
            f"{symbol} exposure would be {exposure_pct:.1f}% of equity, "
            f"over the {limits['MAX_ONE_SYMBOL_EXPOSURE_PCT']}% single-symbol limit."
        )
    return True, ""


def check_pre_trade(symbol: str, entry_price: Decimal, stop_loss: Decimal) -> RiskDecision:
    """
    The single entry point apps.signals.engine calls for every candidate
    trade. Runs every check (not short-circuiting on the first failure)
    so a rejected TradingSignal.reason can explain everything that was
    wrong at once. risk_score reported back is used as one of the three
    inputs to TradingSignal.total_score, alongside technical_score and
    sentiment_score.
    """
    reasons: list[str] = []
    equity = get_equity()

    for check in (
        lambda: _check_kill_switch(),
        lambda: _check_feed_freshness(symbol),
        lambda: _check_drawdown(equity, symbol),
        lambda: _check_daily_loss(equity, symbol),
        lambda: _check_consecutive_losses(equity, symbol),
        lambda: _check_exposure(symbol),
    ):
        ok, reason = check()
        if not ok:
            reasons.append(reason)

    if reasons:
        # Any hard-limit failure is an outright rejection -- risk_score
        # is 0.0 rather than partial credit, since these are veto
        # conditions, not a weighted score (manual: "Risk rules override
        # AI output", not "risk lowers the AI's score").
        return RiskDecision(approved=False, risk_score=0.0, reasons=reasons)

    qty, sizing_note = _compute_position_size(equity, entry_price, stop_loss, symbol)
    if qty <= 0:
        return RiskDecision(
            approved=False, risk_score=0.0,
            reasons=[sizing_note or "Computed position size was zero."],
        )

    exposure_ok, exposure_reason = _check_single_symbol_exposure(symbol, entry_price, qty, equity)
    if not exposure_ok:
        return RiskDecision(approved=False, risk_score=0.0, reasons=[exposure_reason])

    combined_risk_ok, combined_risk_reason = _check_combined_open_risk(
        symbol, entry_price, stop_loss, qty, equity,
    )
    if not combined_risk_ok:
        return RiskDecision(approved=False, risk_score=0.0, reasons=[combined_risk_reason])

    reasons_for_approval = [sizing_note] if sizing_note else []
    return RiskDecision(
        approved=True, risk_score=1.0, reasons=reasons_for_approval, position_size=qty,
    )
