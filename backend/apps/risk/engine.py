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
from django.db import transaction
from django.db.models import Q
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
    today = timezone.localdate()
    from apps.execution.models import ExecutionModeSetting, get_execution_mode

    execution_mode = get_execution_mode()
    with transaction.atomic():
        equity, created = AccountEquity.objects.select_for_update().get_or_create(
            pk=1,
            defaults={
                "current_equity": settings.STARTING_EQUITY,
                "daily_start_equity": settings.STARTING_EQUITY,
                "peak_equity": settings.STARTING_EQUITY,
                "trading_day": today,
                "source_mode": ExecutionModeSetting.Mode.PAPER,
            },
        )
        if (
            not created
            and execution_mode == ExecutionModeSetting.Mode.PAPER
            and equity.source_mode != ExecutionModeSetting.Mode.PAPER
        ):
            # Paper simulation starts a fresh risk baseline when leaving a
            # broker-synchronized live session; it must not mutate live risk
            # history while live execution is armed.
            equity.source_mode = ExecutionModeSetting.Mode.PAPER
            equity.daily_start_equity = equity.current_equity
            equity.peak_equity = equity.current_equity
            equity.consecutive_losses = 0
            equity.trading_day = today
            equity.last_broker_sync_at = None
            equity.save(update_fields=[
                "source_mode", "daily_start_equity", "peak_equity",
                "consecutive_losses", "trading_day", "last_broker_sync_at",
            ])
        if not created and equity.trading_day < today:
            previous_day = equity.trading_day
            equity.daily_start_equity = equity.current_equity
            equity.trading_day = today
            equity.save(update_fields=["daily_start_equity", "trading_day"])
            _log_event(
                "daily_equity_rollover",
                f"Daily equity baseline rolled from {previous_day} to {today}.",
                severity="info",
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


def validate_signal_for_execution(signal) -> tuple[bool, str]:
    """Repeat every volatile hard-risk check immediately before execution."""
    reasons: list[str] = []
    now = timezone.now()
    max_age = timedelta(minutes=getattr(settings, "MAX_SIGNAL_AGE_MINUTES", 10))
    if now - signal.created_at > max_age:
        reasons.append(
            f"Signal is stale ({(now - signal.created_at).total_seconds() / 60:.1f} minutes old)."
        )

    from apps.market_data.market_hours import is_market_open

    market_open, market_reason = is_market_open(at=now)
    if not market_open:
        reasons.append(f"Market is closed: {market_reason}")

    equity = get_equity()
    from apps.execution.models import ExecutionModeSetting, get_execution_mode

    if get_execution_mode() == ExecutionModeSetting.Mode.LIVE:
        if equity.source_mode != ExecutionModeSetting.Mode.LIVE or equity.last_broker_sync_at is None:
            reasons.append("Live account equity has not been synchronized with the broker.")
        elif timezone.localdate(equity.last_broker_sync_at) != timezone.localdate(now):
            reasons.append("Live broker equity synchronization is from a prior trading day.")
    for check in (
        lambda: _check_kill_switch(),
        lambda: _check_feed_freshness(signal.symbol),
        lambda: _check_drawdown(equity, signal.symbol),
        lambda: _check_daily_loss(equity, signal.symbol),
        lambda: _check_consecutive_losses(equity, signal.symbol),
        lambda: _check_exposure(signal.symbol),
    ):
        ok, reason = check()
        if not ok:
            reasons.append(reason)

    qty = int(signal.position_size or 0)
    if qty <= 0:
        reasons.append("Signal quantity is not positive.")
    elif signal.entry_price <= 0 or signal.stop_loss <= 0:
        reasons.append("Signal entry and stop prices must be positive.")
    else:
        capped_qty, cap_reason = _cap_qty_to_single_symbol_exposure(
            signal.symbol, signal.entry_price, qty, equity,
        )
        if capped_qty < qty:
            reasons.append(cap_reason or "Position exceeds the current single-symbol exposure limit.")
        combined_ok, combined_reason = _check_combined_open_risk(
            signal.symbol, signal.entry_price, signal.stop_loss, qty, equity,
        )
        if not combined_ok:
            reasons.append(combined_reason)

    if signal.option_contract_id is not None:
        from apps.options.expiry_service import is_expiry_eligible

        contract = signal.option_contract
        if not contract.is_active or not is_expiry_eligible(contract.expiry, at=now):
            reasons.append(f"Option contract {contract.tradingsymbol} is no longer eligible for entry.")
        if not contract.tradingsymbol or not contract.symbol_token or contract.lot_size <= 0:
            reasons.append("Option contract lacks complete broker instrument metadata.")
        elif qty % contract.lot_size:
            reasons.append(
                f"Quantity {qty} is not a whole multiple of lot size {contract.lot_size}."
            )

        snapshot = contract.snapshots.order_by("-timestamp").first()
        if snapshot is None:
            reasons.append("No option quote snapshot exists for execution-time liquidity validation.")
        elif now - snapshot.timestamp > timedelta(minutes=FEED_STALENESS_THRESHOLD_MINUTES):
            reasons.append("The option quote snapshot is stale.")
        else:
            liquidity_ok, liquidity_reason = check_option_contract_liquidity(
                contract,
                float(snapshot.bid) if snapshot.bid is not None else None,
                float(snapshot.ask) if snapshot.ask is not None else None,
                snapshot.open_interest,
                snapshot.volume,
            )
            if not liquidity_ok:
                reasons.append(liquidity_reason)

    return not reasons, "; ".join(reasons)


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
        from apps.execution.models import get_execution_mode

        if get_execution_mode() == "live":
            return False, "Feed freshness is unknown -- live entries are blocked until a health check succeeds."
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


def _check_equity_provenance(equity: AccountEquity) -> tuple[bool, str]:
    """Live sizing must use a broker-synchronized, same-day baseline."""
    from apps.execution.models import ExecutionModeSetting, get_execution_mode

    if get_execution_mode() != ExecutionModeSetting.Mode.LIVE:
        return True, ""
    if equity.source_mode != ExecutionModeSetting.Mode.LIVE or equity.last_broker_sync_at is None:
        return False, "Live account equity has not been synchronized with the broker."
    if timezone.localdate(equity.last_broker_sync_at) != timezone.localdate():
        return False, "Live broker equity synchronization is from a prior trading day."
    return True, ""


def enforce_account_risk_limits() -> tuple[bool, str]:
    """Evaluate account-wide limits even when no new signal is pending."""
    equity = get_equity()
    reasons = []
    for check in (
        lambda: _check_drawdown(equity, ""),
        lambda: _check_daily_loss(equity, ""),
        lambda: _check_consecutive_losses(equity, ""),
    ):
        ok, reason = check()
        if not ok:
            reasons.append(reason)
    return not reasons, "; ".join(reasons)


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
    open_positions = OpenPosition.objects.filter(closed_at__isnull=True).select_related("signal")
    from apps.signals.models import TradingSignal

    executing_signals = TradingSignal.objects.filter(status="executing")

    if open_positions.count() + executing_signals.count() >= limits["MAX_OPEN_POSITIONS"]:
        return False, (
            f"Already at max open positions ({limits['MAX_OPEN_POSITIONS']})."
        )

    if open_positions.filter(Q(symbol=symbol) | Q(signal__symbol=symbol)).exists():
        return False, f"A position in {symbol} is already open (max one at a time per symbol)."

    if executing_signals.filter(symbol=symbol).exists():
        return False, f"An order for {symbol} is already executing."

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
    _cap_qty_to_single_symbol_exposure already covers separately). Returns
    True (does not block) if the correlation matrix can't be computed
    yet (insufficient historical data) -- same "unknown is not a hard
    block" stance the rest of this module uses for missing data.
    """
    from apps.market_data.correlation import compute_correlation_matrix

    limits = settings.RISK_HARD_LIMITS
    open_positions = OpenPosition.objects.filter(closed_at__isnull=True).select_related("signal")

    new_risk_amount = float(qty) * float(abs(entry_price - stop_loss))
    combined_risk_amount = new_risk_amount

    try:
        matrix = compute_correlation_matrix()
    except Exception:
        matrix = None

    weighted_detail = []
    if matrix is not None and not matrix.empty and symbol in matrix.columns:
        for position in open_positions:
            exposure_symbol = position.signal.symbol
            if exposure_symbol == symbol:
                weight = 1.0  # shouldn't normally happen (one-position-per-symbol), but fully count if it does
            elif exposure_symbol in matrix.columns:
                corr = matrix.at[symbol, exposure_symbol]
                weight = abs(float(corr)) if corr == corr else 0.0  # corr==corr is a NaN guard
            else:
                weight = 0.0  # no correlation data for this open symbol -- don't assume correlated

            if weight <= 0.0:
                continue
            position_risk_amount = float(position.qty) * float(abs(position.entry_price - position.stop_loss))
            contribution = weight * position_risk_amount
            combined_risk_amount += contribution
            if weight >= 0.3:  # only mention meaningfully-correlated contributors in the reason text
                weighted_detail.append(f"{exposure_symbol} (weight {weight:.2f})")
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


def check_option_contract_liquidity(
    contract, bid: float | None, ask: float | None, open_interest: int | None, volume: int | None,
) -> tuple[bool, str]:
    """
    manual section 11's "Risk Engine should validate lot size, liquidity,
    bid/ask spread" applied to one already-resolved option contract --
    called by apps.options.index_direction_strategy right after it
    resolves a real OptionContract, alongside (not instead of) the
    symbol-level check_pre_trade() below. Plain-value signature (bid/
    ask/open_interest/volume, not an OptionChainSnapshot instance) since
    apps.options.strike_selector.suggest_best_strike's candidate dict
    already carries these -- no extra query needed here.

    Two distinct failure phrasings, matching the platform's safety
    rules: "OPTION DATA UNAVAILABLE" when the data needed to judge
    liquidity simply isn't there yet (never invent a spread/liquidity
    verdict from missing data), "TRADE BLOCKED" when real data says the
    contract fails a threshold.
    """
    if not contract.lot_size:
        return False, f"TRADE BLOCKED: lot size unknown for {contract.tradingsymbol or contract}."

    if bid is None or ask is None:
        return False, f"OPTION DATA UNAVAILABLE: no live bid/ask for {contract.tradingsymbol or contract}."

    mid = (bid + ask) / 2
    if mid <= 0:
        return False, f"OPTION DATA UNAVAILABLE: non-positive bid/ask midpoint for {contract.tradingsymbol or contract}."

    limits = settings.RISK_HARD_LIMITS
    spread_pct = (ask - bid) / mid * 100
    if spread_pct > limits["MAX_OPTION_BID_ASK_SPREAD_PCT"]:
        return False, (
            f"TRADE BLOCKED: bid/ask spread {spread_pct:.1f}% on {contract.tradingsymbol or contract} "
            f"exceeds the {limits['MAX_OPTION_BID_ASK_SPREAD_PCT']}% limit."
        )

    if (open_interest or 0) < limits["MIN_OPTION_OPEN_INTEREST"]:
        return False, (
            f"TRADE BLOCKED: open interest {open_interest or 0} on {contract.tradingsymbol or contract} "
            f"is below the {limits['MIN_OPTION_OPEN_INTEREST']} minimum."
        )

    if (volume or 0) < limits["MIN_OPTION_VOLUME"]:
        return False, (
            f"TRADE BLOCKED: volume {volume or 0} on {contract.tradingsymbol or contract} "
            f"is below the {limits['MIN_OPTION_VOLUME']} minimum."
        )

    return True, ""


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

    # str() before Decimal(): risk_pct is a plain float (from
    # settings.RISK_HARD_LIMITS times a float drawdown/expiry
    # multiplier above) -- Decimal(float) reproduces that float's exact
    # binary representation (e.g. Decimal(0.1) == Decimal('0.1000000
    # 000000000055511151231257827021181583404541015625')), same trap
    # apps.signals.engine.generate_signal's entry_price/stop_loss
    # already route through str() first to avoid.
    risk_amount = equity.current_equity * (Decimal(str(risk_pct)) / Decimal(100))
    stop_distance = abs(entry_price - stop_loss)

    if stop_distance <= 0:
        return 0, "Stop distance is zero or negative -- cannot size the position."

    qty = int(risk_amount / stop_distance)
    return max(qty, 0), "; ".join(notes)


def _cap_qty_to_single_symbol_exposure(
    symbol: str, entry_price: Decimal, qty: int, equity: AccountEquity,
) -> tuple[int, str]:
    """
    manual section 13's "Exposure rules" single-symbol cap, applied as a
    SIZE CAP rather than a pass/fail veto on the risk-based qty
    _compute_position_size already produced. The two constraints
    (risk % per trade, max % notional in one symbol) are independent
    budgets -- a trade whose risk-based qty happens to cost more than
    the exposure cap can still be taken at a SMALLER size that fits both
    budgets at once; rejecting it outright (the previous behavior of
    this check, back when it was named _check_single_symbol_exposure)
    throws away a perfectly good trade for no reason a real risk desk
    would recognize. This mirrors _compute_position_size's own
    "reduce size" posture for drawdown/expiry-day, applied to the
    exposure budget too, instead of being the one constraint in this
    module that vetoes instead of sizing down.

    Only returns qty=0 (a genuine rejection, not just a smaller size)
    when even ONE unit at `entry_price` already costs more than the
    exposure budget -- that trade truly cannot be taken at any size.
    """
    limits = settings.RISK_HARD_LIMITS
    if entry_price <= 0 or not equity.current_equity:
        return qty, ""

    max_notional = equity.current_equity * (Decimal(str(limits["MAX_ONE_SYMBOL_EXPOSURE_PCT"])) / Decimal(100))
    max_affordable_qty = int(max_notional / entry_price)

    if max_affordable_qty <= 0:
        return 0, (
            f"{symbol} entry price {entry_price} alone exceeds the "
            f"{limits['MAX_ONE_SYMBOL_EXPOSURE_PCT']}% single-symbol exposure limit of the "
            f"current {equity.current_equity} equity -- cannot buy even one unit within budget."
        )

    if max_affordable_qty < qty:
        return max_affordable_qty, (
            f"Position size capped to {max_affordable_qty} (from {qty}) by the "
            f"{limits['MAX_ONE_SYMBOL_EXPOSURE_PCT']}% single-symbol exposure limit for {symbol}."
        )

    return qty, ""


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
        lambda: _check_equity_provenance(equity),
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

    qty, exposure_note = _cap_qty_to_single_symbol_exposure(symbol, entry_price, qty, equity)
    if qty <= 0:
        return RiskDecision(approved=False, risk_score=0.0, reasons=[exposure_note])

    # Combined open risk is evaluated against the FINAL (possibly
    # exposure-capped) qty above, not the pre-cap risk-based qty -- it
    # must judge the actual size this trade would be taken at.
    combined_risk_ok, combined_risk_reason = _check_combined_open_risk(
        symbol, entry_price, stop_loss, qty, equity,
    )
    if not combined_risk_ok:
        return RiskDecision(approved=False, risk_score=0.0, reasons=[combined_risk_reason])

    reasons_for_approval = [n for n in (sizing_note, exposure_note) if n]
    return RiskDecision(
        approved=True, risk_score=1.0, reasons=reasons_for_approval, position_size=qty,
    )
