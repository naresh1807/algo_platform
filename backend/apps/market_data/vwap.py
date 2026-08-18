"""
VWAP (Volume-Weighted Average Price) -- a real computation over
already-ingested intraday apps.market_data.HistoricalData candles, not
a stub: this platform has no tick-level trade feed, but it does ingest
OHLCV candles (apps.market_data.tasks.ingest_watchlist_candles), and
typical-price-times-volume over those candles is the standard VWAP
definition used whenever individual trade prints aren't available.

Session boundary is apps.market_data.market_hours.MARKET_OPEN_TIME --
VWAP resets every trading day, same convention every real trading
platform uses (a multi-day cumulative VWAP would mix unrelated
sessions together and mean nothing).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time

from django.utils import timezone

from .market_hours import MARKET_OPEN_TIME

SESSION_END_TIME = time(23, 59, 59)  # upper bound only -- real candles never exist past NSE's own close


def _session_candles(symbol: str, timeframe: str, for_date: date):
    from .models import HistoricalData

    session_start = timezone.make_aware(datetime.combine(for_date, MARKET_OPEN_TIME))
    session_end = timezone.make_aware(datetime.combine(for_date, SESSION_END_TIME))
    return list(
        HistoricalData.objects.filter(
            symbol=symbol, timeframe=timeframe, timestamp__gte=session_start, timestamp__lte=session_end,
        ).order_by("timestamp")
    )


def calculate_vwap(symbol: str, timeframe: str = "5m", for_date: date | None = None) -> float | None:
    """
    cumulative(typical_price * volume) / cumulative(volume) over one
    trading day's candles, typical_price = (high+low+close)/3 per
    candle (the standard VWAP input when only OHLCV bars, not
    individual trades, are available). Returns None (not 0) if no
    candles exist yet for that day/timeframe -- 0 would misleadingly
    read as "price is zero", not "no data yet".
    """
    for_date = for_date or timezone.localdate()
    candles = _session_candles(symbol, timeframe, for_date)
    if not candles:
        return None

    cumulative_pv = 0.0
    cumulative_volume = 0
    for c in candles:
        if c.volume <= 0:
            continue
        typical_price = float(c.high + c.low + c.close) / 3
        cumulative_pv += typical_price * c.volume
        cumulative_volume += c.volume

    if cumulative_volume == 0:
        return None
    return round(cumulative_pv / cumulative_volume, 4)


def calculate_vwap_with_bands(symbol: str, timeframe: str = "5m", for_date: date | None = None, num_std: float = 1.0) -> dict:
    """
    VWAP plus volume-weighted standard-deviation bands (upper/lower) --
    the same construction most charting platforms use for "VWAP bands":
    variance is the volume-weighted variance of each candle's typical
    price around the VWAP itself, not a naive unweighted std-dev, so a
    high-volume candle far from VWAP counts more than a thin one.

    Returns {"vwap", "upper_band", "lower_band", "candle_count"} --
    every value None if there's no data yet.
    """
    for_date = for_date or timezone.localdate()
    candles = _session_candles(symbol, timeframe, for_date)
    if not candles:
        return {"vwap": None, "upper_band": None, "lower_band": None, "candle_count": 0}

    cumulative_pv = 0.0
    cumulative_volume = 0
    typical_prices = []
    for c in candles:
        if c.volume <= 0:
            continue
        typical_price = float(c.high + c.low + c.close) / 3
        cumulative_pv += typical_price * c.volume
        cumulative_volume += c.volume
        typical_prices.append((typical_price, c.volume))

    if cumulative_volume == 0:
        return {"vwap": None, "upper_band": None, "lower_band": None, "candle_count": 0}

    vwap = cumulative_pv / cumulative_volume
    weighted_sq_diff = sum(v * (tp - vwap) ** 2 for tp, v in typical_prices)
    variance = weighted_sq_diff / cumulative_volume
    std_dev = math.sqrt(variance)

    return {
        "vwap": round(vwap, 4),
        "upper_band": round(vwap + num_std * std_dev, 4),
        "lower_band": round(vwap - num_std * std_dev, 4),
        "candle_count": len(typical_prices),
    }
