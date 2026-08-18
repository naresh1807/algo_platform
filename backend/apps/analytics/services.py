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
from common.constants import SignalStatus
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
