"""
Computes apps.analytics.PerformanceMetrics for a given trading day from
real trade data (closed OpenPosition rows + TradingSignal rows for that
date) -- this is what apps.learning.tasks.run_daily_review reads before
drafting a DailyReviewNote, and it's the same data
apps.learning.tasks.check_for_drift's win-rate comparison could
eventually be pointed at instead of recomputing win-rate itself
(currently check_for_drift computes its own rolling win-rate directly
from OpenPosition rather than reading PerformanceMetrics -- left as-is
rather than unified, since drift detection needs a rolling N-trades
window regardless of calendar-day boundaries, while PerformanceMetrics
is explicitly per-day).

Also home to compute_max_drawdown_for_day and compute_sharpe_ratio,
both powered by apps.risk.models.EquitySnapshot -- the equity curve
apps.risk.signals now records automatically as AccountEquity changes.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Q

from apps.execution.models import OpenPosition
from apps.signals.models import TradingSignal
from common.constants import SignalStatus, SignalType
from .models import PerformanceMetrics


def compute_max_drawdown_for_day(for_date: date) -> float | None:
    """
    Real intraday max drawdown (peak-to-trough %, within just this
    calendar day) from apps.risk.models.EquitySnapshot -- the equity
    curve apps.risk.signals now records automatically every time
    AccountEquity changes. Returns None if fewer than 2 snapshots exist
    for the day (nothing happened, or this predates EquitySnapshot
    existing) -- same "can't compute yet, don't fabricate" stance as
    every other None-returning function in this app.
    """
    from apps.risk.models import EquitySnapshot

    day_start, day_end = for_date, for_date + timedelta(days=1)
    snapshots = list(
        EquitySnapshot.objects.filter(timestamp__gte=day_start, timestamp__lt=day_end).order_by("timestamp")
    )
    if len(snapshots) < 2:
        return None

    peak = float(snapshots[0].equity)
    max_dd = 0.0
    for s in snapshots:
        equity = float(s.equity)
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak * 100
            max_dd = max(max_dd, dd)
    return round(max_dd, 4)


def compute_sharpe_ratio(lookback_days: int = 30, risk_free_rate_annual: float = 0.065) -> float | None:
    """
    Annualized Sharpe ratio over the trailing `lookback_days` calendar
    days, from apps.risk.models.EquitySnapshot's daily closing equity
    (the last snapshot recorded on each day). Deliberately NOT stored
    on PerformanceMetrics -- that model is one row per single day, and
    a Sharpe ratio is inherently a rolling, multi-day statistic (the
    same reason apps.learning.tasks.check_for_drift computes its own
    rolling window rather than reading single-day PerformanceMetrics
    rows) -- so this is computed fresh on each call instead.

    risk_free_rate_annual: same documented-assumption INR proxy as
    apps.options.greeks.DEFAULT_RISK_FREE_RATE (kept as a separate
    constant here rather than importing across apps, since Sharpe's
    use of it -- subtracted from returns -- is conceptually unrelated
    to options pricing's use of it as a discount rate).

    Returns None if there are fewer than 3 distinct trading days of
    equity history in the window (a Sharpe ratio from 1-2 data points
    is not a meaningful number, just noise dressed up as a statistic).
    """
    import statistics

    from django.utils import timezone

    from apps.risk.models import EquitySnapshot

    window_start = timezone.now() - timedelta(days=lookback_days)
    snapshots = list(
        EquitySnapshot.objects.filter(timestamp__gte=window_start).order_by("timestamp")
    )
    if not snapshots:
        return None

    # One closing equity value per calendar day (last snapshot seen
    # that day), so intraday noise doesn't inflate the return series'
    # variance -- Sharpe is meant to measure day-to-day account
    # volatility, not tick-to-tick.
    daily_closes: dict[date, float] = {}
    for s in snapshots:
        daily_closes[timezone.localtime(s.timestamp).date()] = float(s.equity)

    days = sorted(daily_closes)
    if len(days) < 3:
        return None

    daily_returns = []
    for prev_day, curr_day in zip(days, days[1:]):
        prev_equity = daily_closes[prev_day]
        curr_equity = daily_closes[curr_day]
        if prev_equity > 0:
            daily_returns.append((curr_equity - prev_equity) / prev_equity)

    if len(daily_returns) < 2:
        return None

    daily_rf = risk_free_rate_annual / 252
    excess_returns = [r - daily_rf for r in daily_returns]

    mean_excess = statistics.mean(excess_returns)
    stdev_excess = statistics.pstdev(excess_returns)
    if stdev_excess == 0:
        return None

    return round((mean_excess / stdev_excess) * (252 ** 0.5), 4)


def compute_sortino_ratio(
    lookback_days: int = 30, risk_free_rate_annual: float = 0.065, target_return: float = 0.0,
) -> float | None:
    """
    Same daily-equity-return series as compute_sharpe_ratio above, but
    the denominator is DOWNSIDE deviation only (stdev of the returns
    that fall below `target_return`, default 0) instead of total
    volatility -- Sortino doesn't penalize the upside volatility a
    right-skewed strategy is supposed to have (this platform's own
    ATR-based 1R/2R target sizing is exactly that shape: bigger wins
    than losses by design), which a symmetric Sharpe denominator
    unfairly counts against it.

    Returns None under the same "not enough history" conditions as
    compute_sharpe_ratio, PLUS when there are zero downside
    observations in the window -- that's "no losing days to measure
    from," not the same as a genuinely-zero downside deviation (which
    would imply an infinite, meaningless ratio).
    """
    import statistics

    from django.utils import timezone

    from apps.risk.models import EquitySnapshot

    window_start = timezone.now() - timedelta(days=lookback_days)
    snapshots = list(EquitySnapshot.objects.filter(timestamp__gte=window_start).order_by("timestamp"))
    if not snapshots:
        return None

    daily_closes: dict[date, float] = {}
    for s in snapshots:
        daily_closes[timezone.localtime(s.timestamp).date()] = float(s.equity)

    days = sorted(daily_closes)
    if len(days) < 3:
        return None

    daily_returns = []
    for prev_day, curr_day in zip(days, days[1:]):
        prev_equity = daily_closes[prev_day]
        curr_equity = daily_closes[curr_day]
        if prev_equity > 0:
            daily_returns.append((curr_equity - prev_equity) / prev_equity)
    if len(daily_returns) < 2:
        return None

    daily_rf = risk_free_rate_annual / 252
    excess_returns = [r - daily_rf for r in daily_returns]

    downside_returns = [r for r in excess_returns if r < target_return]
    if not downside_returns:
        return None

    downside_deviation = statistics.pstdev(downside_returns) if len(downside_returns) > 1 else abs(downside_returns[0])
    if downside_deviation == 0:
        return None

    mean_excess = statistics.mean(excess_returns)
    return round((mean_excess / downside_deviation) * (252 ** 0.5), 4)


def compute_calmar_ratio(lookback_days: int = 30) -> float | None:
    """
    Annualized return / max drawdown over the trailing window -- the
    standard Calmar definition. Annualized return comes from actual
    compound equity growth across the window's real first/last
    snapshots (not a mean-daily-return approximation); max drawdown
    reuses the same EquitySnapshot peak-to-trough logic compute_max_
    drawdown_for_day already uses, just over the whole window instead
    of a single calendar day.

    Returns None if fewer than 2 distinct trading days exist in the
    window, or if max drawdown is exactly 0 -- a real "never drew down
    at all" case makes Calmar undefined (division by zero), not a
    number worth reporting.
    """
    from django.utils import timezone

    from apps.risk.models import EquitySnapshot

    window_start = timezone.now() - timedelta(days=lookback_days)
    snapshots = list(EquitySnapshot.objects.filter(timestamp__gte=window_start).order_by("timestamp"))
    if len(snapshots) < 2:
        return None

    first_equity = float(snapshots[0].equity)
    last_equity = float(snapshots[-1].equity)
    if first_equity <= 0:
        return None

    days_elapsed = max((snapshots[-1].timestamp - snapshots[0].timestamp).days, 1)
    total_return = (last_equity / first_equity) - 1
    annualized_return = (1 + total_return) ** (365 / days_elapsed) - 1

    peak = first_equity
    max_dd = 0.0
    for s in snapshots:
        equity = float(s.equity)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    if max_dd == 0:
        return None

    return round(annualized_return / max_dd, 4)


def compute_daily_performance(for_date: date) -> PerformanceMetrics:
    """
    update_or_create rather than create: re-running this for a date
    that's already been computed (e.g. the daily review task being
    retried, or a manual re-run after a data correction) should replace
    the row, not create a duplicate or error on the unique `date`
    constraint.
    """
    day_start = for_date
    day_end = for_date + timedelta(days=1)

    closed_positions = list(
        OpenPosition.objects.filter(
            closed_at__gte=day_start, closed_at__lt=day_end,
        )
    )

    total_trades = len(closed_positions)
    wins = [p for p in closed_positions if p.unrealized_pnl > 0]
    losses = [p for p in closed_positions if p.unrealized_pnl <= 0]

    win_rate = len(wins) / total_trades if total_trades else None

    gross_profit = sum(float(p.unrealized_pnl) for p in wins)
    gross_loss = abs(sum(float(p.unrealized_pnl) for p in losses))
    # profit_factor is conventionally undefined (or infinite) with zero
    # losses -- reported as None rather than a fake infinity/zero so the
    # dashboard can show "—" instead of a misleading number.
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    # Expectancy / avg_r require knowing each trade's risk in R terms
    # (entry-to-stop distance) -- computed per-trade from the linked
    # signal rather than assuming a fixed R, since ATR-based stops mean
    # every trade's dollar-risk differs.
    r_multiples = []
    for p in closed_positions:
        risk_per_unit = abs(p.entry_price - p.signal.stop_loss)
        if risk_per_unit > 0:
            r_multiples.append(float(p.unrealized_pnl) / float(risk_per_unit * p.qty))
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else None
    expectancy = avg_r  # same figure -- avg R per trade IS expectancy in R terms

    rejected_that_day = TradingSignal.objects.filter(
        created_at__gte=day_start, created_at__lt=day_end, status=SignalStatus.REJECTED,
    ).count()
    all_signals_that_day = TradingSignal.objects.filter(
        created_at__gte=day_start, created_at__lt=day_end,
    ).count()
    false_signal_rate = (
        rejected_that_day / all_signals_that_day if all_signals_that_day else None
    )

    # max_drawdown now computed for real from apps.risk.EquitySnapshot
    # (see compute_max_drawdown_for_day above) -- the equity curve that
    # used to not exist. slippage stays None: it needs the signal's
    # intended entry_price compared against the executor's actual fill
    # price, which live_executor records but paper_executor doesn't
    # (paper fills are always exactly the intended price by
    # construction) -- that remains a real, documented gap.
    net_pnl = round(gross_profit - gross_loss, 2) if total_trades else None

    metrics, _created = PerformanceMetrics.objects.update_or_create(
        date=for_date,
        defaults={
            "total_trades": total_trades,
            "net_pnl": net_pnl,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "avg_r": avg_r,
            "max_drawdown": compute_max_drawdown_for_day(for_date),
            "false_signal_rate": false_signal_rate,
        },
    )
    return metrics


# ---------------------------------------------------------------------------
# Performance breakdowns (manual section 18 dashboard, "by dimension" view).
# Every function below groups already-closed OpenPosition rows (real trades,
# never a live recomputation) by some dimension already stored on the
# position's own signal/contract -- no new data source, same "read what
# actually happened" posture as compute_daily_performance above. Each
# returns a list of dicts, one per group, sorted by group key, omitting
# groups with zero trades (an empty group is noise, not a data point).
# ---------------------------------------------------------------------------


def _aggregate_position_stats(positions: list[OpenPosition]) -> dict:
    """
    Shared win_rate/net_pnl/avg_r/profit_factor arithmetic, factored out
    of compute_daily_performance's inline version above so every
    breakdown function below computes these identically -- deliberately
    NOT used to rewrite compute_daily_performance itself (that function's
    existing rounding/field set is already covered by its own tests;
    changing what it returns is out of scope here).
    """
    total = len(positions)
    if not total:
        return {"trade_count": 0, "win_rate": None, "net_pnl": None, "avg_r": None, "profit_factor": None}

    wins = [p for p in positions if p.unrealized_pnl > 0]
    losses = [p for p in positions if p.unrealized_pnl <= 0]

    gross_profit = sum(float(p.unrealized_pnl) for p in wins)
    gross_loss = abs(sum(float(p.unrealized_pnl) for p in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    r_multiples = []
    for p in positions:
        risk_per_unit = abs(p.entry_price - p.signal.stop_loss)
        if risk_per_unit > 0:
            r_multiples.append(float(p.unrealized_pnl) / float(risk_per_unit * p.qty))
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else None

    return {
        "trade_count": total,
        "win_rate": round(len(wins) / total, 4),
        "net_pnl": round(gross_profit - gross_loss, 2),
        "avg_r": round(avg_r, 4) if avg_r is not None else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
    }


def _closed_positions_in_window(lookback_days: int) -> list[OpenPosition]:
    from django.utils import timezone

    window_start = timezone.now() - timedelta(days=lookback_days)
    return list(
        OpenPosition.objects.filter(closed_at__gte=window_start, closed_at__isnull=False)
        .select_related("signal", "option_contract")
    )


def compute_regime_breakdown(lookback_days: int = 30) -> list[dict]:
    """Closed-trade performance grouped by apps.signals.models.TradingSignal.regime at signal-creation time."""
    by_regime: dict[str, list[OpenPosition]] = {}
    for p in _closed_positions_in_window(lookback_days):
        by_regime.setdefault(p.signal.regime, []).append(p)
    return [{"regime": regime, **_aggregate_position_stats(group)} for regime, group in sorted(by_regime.items())]


def compute_expiry_breakdown(lookback_days: int = 30) -> list[dict]:
    """
    Closed-trade performance grouped by the resolved OptionContract's expiry
    date -- only positions with option_contract set (a real option-premium
    position, not the underlying-index proxy) are counted; every other
    position is silently excluded rather than bucketed under a fake "no
    expiry" group.
    """
    by_expiry: dict[str, list[OpenPosition]] = {}
    for p in _closed_positions_in_window(lookback_days):
        if p.option_contract is None:
            continue
        by_expiry.setdefault(p.option_contract.expiry.isoformat(), []).append(p)
    return [{"expiry": expiry, **_aggregate_position_stats(group)} for expiry, group in sorted(by_expiry.items())]


def compute_option_side_breakdown(lookback_days: int = 30) -> list[dict]:
    """
    Closed-trade performance grouped by CE/PE (apps.signals.models.
    TradingSignal.option_side). Positions from the underlying/index engine
    (apps.signals.engine), which never sets option_side, are grouped under
    "UNDERLYING" rather than dropped -- that's a real, meaningful category
    of trade, not missing data.
    """
    by_side: dict[str, list[OpenPosition]] = {}
    for p in _closed_positions_in_window(lookback_days):
        side = p.signal.option_side or "UNDERLYING"
        by_side.setdefault(side, []).append(p)
    return [{"option_side": side, **_aggregate_position_stats(group)} for side, group in sorted(by_side.items())]


# Only the strategies apps.options.strategy_selector.EXECUTABLE_STRATEGIES
# actually allows this platform to open today -- every option_side maps
# 1:1 onto one of them, so this reuses that same mapping rather than
# re-deriving a "strategy" the execution layer never actually chose.
_OPTION_SIDE_TO_STRATEGY = {"CE": "LONG_CALL", "PE": "LONG_PUT"}


def compute_strategy_breakdown(lookback_days: int = 30) -> list[dict]:
    """
    Closed-trade performance grouped by the executed strategy. Since only
    LONG_CALL/LONG_PUT/the underlying itself are ever actually executable
    (apps.options.strategy_selector.EXECUTABLE_STRATEGIES), this is derived
    directly from option_side rather than re-classifying each historical
    trade against today's strategy_selector logic (which needs live
    regime/IV-rank/expected-move inputs that no longer exist for a closed
    trade opened days ago).
    """
    by_strategy: dict[str, list[OpenPosition]] = {}
    for p in _closed_positions_in_window(lookback_days):
        strategy = _OPTION_SIDE_TO_STRATEGY.get(p.signal.option_side, "UNDERLYING")
        by_strategy.setdefault(strategy, []).append(p)
    return [{"strategy": strategy, **_aggregate_position_stats(group)} for strategy, group in sorted(by_strategy.items())]


def compute_time_of_day_breakdown(lookback_days: int = 30) -> list[dict]:
    """
    Closed-trade performance grouped by which apps.market_data.time_of_day.
    SESSION_PHASES window the position was OPENED in (local IST time) --
    reuses that module's own phase boundaries rather than a second
    definition, so "opening"/"morning"/etc. mean the same thing here as
    everywhere else on the platform. A position opened outside every
    defined phase (shouldn't happen during market hours, but real data can
    surprise you) is grouped under "unknown" rather than dropped.
    """
    from django.utils import timezone

    from apps.market_data.time_of_day import SESSION_PHASES

    by_phase: dict[str, list[OpenPosition]] = {}
    for p in _closed_positions_in_window(lookback_days):
        local_time = timezone.localtime(p.opened_at).time()
        phase = next((name for name, start, end in SESSION_PHASES if start <= local_time < end), "unknown")
        by_phase.setdefault(phase, []).append(p)
    return [{"phase": phase, **_aggregate_position_stats(group)} for phase, group in sorted(by_phase.items())]


def compute_no_trade_rate(lookback_days: int = 30) -> dict:
    """
    Fraction of every TradingSignal created in the window that resolved to
    NO_TRADE -- the no-trade engine's own honesty check: a healthy pipeline
    should show real signals AND real no-trades, not either extreme (100%
    no-trade means something upstream is broken; 0% no-trade means the
    quality gates aren't gating anything).
    """
    from django.utils import timezone

    window_start = timezone.now() - timedelta(days=lookback_days)
    signals = TradingSignal.objects.filter(created_at__gte=window_start)
    total = signals.count()
    no_trade = signals.filter(signal_type=SignalType.NO_TRADE).count()
    return {
        "lookback_days": lookback_days,
        "total_signals": total,
        "no_trade_count": no_trade,
        "no_trade_rate": round(no_trade / total, 4) if total else None,
    }
