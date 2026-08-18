"""
Time-of-Day Engine: session-phase classification (opening/morning/
midday/afternoon/closing) plus REAL historical-baseline comparisons of
each phase's own typical volatility -- built entirely from already-
ingested apps.market_data.HistoricalData candles (grouped by local
time-of-day across recent trading days), not a universal hard-coded
assumption. "Opening is more volatile than midday" is a common claim,
but this module checks it against THIS symbol's own actual history
rather than asserting it -- see the docstring on
_historical_baseline_for_bucket.
"""

from __future__ import annotations

from datetime import date, datetime, time

from django.utils import timezone

from .market_hours import is_market_open

# Deliberately distinct from market_hours.MARKET_OPEN_TIME/CLOSE_TIME
# (which only answer "is the market open") -- these subdivide the
# regular 09:15-15:30 session into named phases a trader would
# recognize, each independently tunable here in one place.
SESSION_PHASES = (
    ("opening", time(9, 15), time(9, 45)),
    ("morning", time(9, 45), time(11, 30)),
    ("midday", time(11, 30), time(13, 30)),
    ("afternoon", time(13, 30), time(15, 0)),
    ("closing", time(15, 0), time(15, 30)),
)

DEFAULT_LOOKBACK_DAYS = 10
ELEVATED_RATIO_THRESHOLD = 1.3
COMPRESSED_RATIO_THRESHOLD = 0.7


def calculate_time_of_day_regime(at: datetime | None = None) -> dict:
    """Which named session phase `at` (default: now) falls in -- "closed" outside market hours, using apps.market_data.market_hours.is_market_open as the single source of truth for that boundary."""
    moment = at or timezone.now()
    is_open, reason = is_market_open(moment)
    if not is_open:
        return {"phase": "closed", "detail": reason}

    ist_moment = timezone.localtime(moment, timezone.get_default_timezone())
    local_time = ist_moment.time()
    for phase_name, start, end in SESSION_PHASES:
        if start <= local_time < end:
            return {
                "phase": phase_name,
                "detail": f"{local_time.strftime('%H:%M')} IST falls in the {phase_name} window ({start.strftime('%H:%M')}-{end.strftime('%H:%M')}).",
            }
    return {"phase": "unknown", "detail": f"{local_time.strftime('%H:%M')} IST is inside market hours but doesn't match any defined session phase."}


def _range_pct_for_bucket(symbol: str, timeframe: str, start_time: time, end_time: time, for_date: date) -> float | None:
    """Average (high-low)/close, as a %, across every candle of `timeframe` whose LOCAL time-of-day falls in [start_time, end_time) on `for_date`. None if no candles exist for that window."""
    from .models import HistoricalData

    window_start = timezone.make_aware(datetime.combine(for_date, start_time))
    window_end = timezone.make_aware(datetime.combine(for_date, end_time))
    candles = HistoricalData.objects.filter(symbol=symbol, timeframe=timeframe, timestamp__gte=window_start, timestamp__lt=window_end)

    ranges = [float((c.high - c.low) / c.close) * 100 for c in candles if c.close]
    if not ranges:
        return None
    return sum(ranges) / len(ranges)


def _historical_baseline_for_bucket(
    symbol: str, timeframe: str, start_time: time, end_time: time,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS, exclude_date: date | None = None,
) -> float | None:
    """
    Average of _range_pct_for_bucket across the last `lookback_days`
    DISTINCT trading days that actually have candles for this symbol/
    timeframe (excluding `exclude_date`, normally today -- today's own
    reading must never leak into its own baseline). This is the "use
    historical baselines rather than fixed thresholds" requirement
    applied literally: no assumption about which session phase is
    "normally" more volatile is hard-coded anywhere in this module.
    """
    exclude_date = exclude_date or timezone.localdate()
    from .models import HistoricalData

    distinct_dates = list(
        HistoricalData.objects.filter(symbol=symbol, timeframe=timeframe)
        .exclude(timestamp__date=exclude_date)
        .order_by("-timestamp").values_list("timestamp__date", flat=True).distinct()[:lookback_days]
    )
    if not distinct_dates:
        return None

    daily_values = []
    for d in distinct_dates:
        value = _range_pct_for_bucket(symbol, timeframe, start_time, end_time, d)
        if value is not None:
            daily_values.append(value)
    if not daily_values:
        return None
    return sum(daily_values) / len(daily_values)


def _detect_bucket_volatility(
    symbol: str, timeframe: str, phase_name: str, start_time: time, end_time: time, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    today = timezone.localdate()
    current = _range_pct_for_bucket(symbol, timeframe, start_time, end_time, today)
    baseline = _historical_baseline_for_bucket(symbol, timeframe, start_time, end_time, lookback_days, exclude_date=today)

    if current is None or baseline is None or baseline == 0:
        return {
            "state": "unavailable", "ratio": None, "current_range_pct": current, "baseline_range_pct": baseline,
            "detail": f"Not enough same-session-phase ({phase_name}) history yet to compare against.",
        }

    ratio = current / baseline
    if ratio >= ELEVATED_RATIO_THRESHOLD:
        state = "elevated"
    elif ratio <= COMPRESSED_RATIO_THRESHOLD:
        state = "compressed"
    else:
        state = "normal"

    return {
        "state": state, "ratio": round(ratio, 2),
        "current_range_pct": round(current, 4), "baseline_range_pct": round(baseline, 4),
        "detail": f"{phase_name} range {current:.3f}% today vs. this symbol's own {phase_name} baseline {baseline:.3f}% ({ratio:.2f}x, {lookback_days}-day lookback).",
    }


def detect_opening_volatility(symbol: str, timeframe: str = "5m", lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    return _detect_bucket_volatility(symbol, timeframe, "opening", time(9, 15), time(9, 45), lookback_days)


def detect_midday_compression(symbol: str, timeframe: str = "5m", lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    return _detect_bucket_volatility(symbol, timeframe, "midday", time(11, 30), time(13, 30), lookback_days)


def detect_closing_volatility(symbol: str, timeframe: str = "5m", lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    return _detect_bucket_volatility(symbol, timeframe, "closing", time(15, 0), time(15, 30), lookback_days)
