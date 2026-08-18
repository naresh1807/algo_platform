"""
Multi-Timeframe Regime Engine -- combines apps.market_data.regime.
classify_regime's existing single-timeframe read across SEVERAL
already-ingested timeframes into a richer composite classification.

Deliberately a SEPARATE, non-persisted analytical read -- NOT a
replacement for TradingSignal.regime (common.constants.MarketRegime's
existing 3-value TRENDING/SIDEWAYS/HIGH_VOLATILITY enum), which many
other things already depend on (apps.market_data.regime.
regime_size_multiplier's position sizing, apps.learning.ml_features'
one-hot regime column, the existing test suite). Changing that shared,
DB-backed enum would be exactly the kind of invasive rebuild this
platform is not supposed to have. This module's richer categories
(TRENDING_BULLISH/BEARISH, LOW_VOLATILITY, BREAKOUT, BREAKDOWN,
MEAN_REVERSION, EVENT_DRIVEN, UNDEFINED) are plain strings for
DISPLAY/ANALYSIS consumers that want more granularity, not a new
database column.

EVENT_DRIVEN is in the output vocabulary but this module can never
detect it with real confidence -- no economic/event calendar exists in
this platform (see apps.market_data.event_risk). It's only ever
reported when volatility is unusually and SIMULTANEOUSLY elevated
across every available timeframe with no confirmed breakout/breakdown
direction, always at low confidence with an explicit "unconfirmed"
caveat -- a real event often causes exactly this pattern, but this
module has no way to verify one is actually happening.
"""

from __future__ import annotations

from common.constants import MarketRegime

from .indicators import compute_indicators
from .regime import BB_WIDTH_HIGH_VOL_THRESHOLD, classify_regime

COMPOSITE_REGIMES = (
    "TRENDING_BULLISH", "TRENDING_BEARISH", "SIDEWAYS", "HIGH_VOLATILITY",
    "LOW_VOLATILITY", "BREAKOUT", "BREAKDOWN", "MEAN_REVERSION", "EVENT_DRIVEN", "UNDEFINED",
)

# Well below classify_regime's own HIGH_VOLATILITY threshold -- a
# distinct "quiet, compressed" band, not just "not high volatility".
LOW_VOLATILITY_BB_WIDTH_THRESHOLD = 0.015

# How far RSI has to sit from 50 to count as an "extreme" (overbought/
# oversold) reading for the mean-reversion read below.
RSI_EXTREME_LOW = 35
RSI_EXTREME_HIGH = 65

# A much higher bar than classify_regime's own HIGH_VOLATILITY BB-width
# threshold -- EVENT_DRIVEN is only ever floated for a genuinely
# extreme, synchronized reading, not merely "somewhat volatile".
EVENT_DRIVEN_BB_WIDTH_MULTIPLE = 2.0

# A representative subset of settings.CHART_TIMEFRAMES -- looping every
# ingested timeframe (1m through 1d) on every call would be 8 separate
# compute_indicators calls for marginal extra signal; these four span
# short/medium/long enough to make "confirmed across timeframes"
# meaningful without that cost.
DEFAULT_TIMEFRAMES = ("5m", "15m", "1h", "1d")


def classify_composite_regime(symbol: str, timeframes: tuple[str, ...] | None = None) -> dict:
    """
    Runs classify_regime independently per timeframe and combines by
    majority vote -- a trend that only shows up on one timeframe is
    noise; one confirmed across most of DEFAULT_TIMEFRAMES is a real
    read. Never raises: a timeframe without enough candle history yet
    is simply excluded from the vote (reported in
    "unavailable_timeframes"), not treated as an error.

    Returns {"regime": one of COMPOSITE_REGIMES, "confidence": 0..1,
    "detail": str, "per_timeframe": {tf: base_regime | None},
    "unavailable_timeframes": [tf, ...]}.
    """
    timeframes = timeframes or DEFAULT_TIMEFRAMES
    per_timeframe: dict[str, str | None] = {}
    per_timeframe_indicators: dict[str, dict] = {}

    for tf in timeframes:
        ind = compute_indicators(symbol, tf)
        if ind is None:
            per_timeframe[tf] = None
            continue
        per_timeframe_indicators[tf] = ind
        per_timeframe[tf] = classify_regime(ind)

    available = {tf: r for tf, r in per_timeframe.items() if r is not None}
    unavailable = [tf for tf, r in per_timeframe.items() if r is None]

    if not available:
        return {
            "regime": "UNDEFINED", "confidence": 0.0,
            "detail": "No timeframe has enough candle history yet.",
            "per_timeframe": per_timeframe, "unavailable_timeframes": unavailable,
        }

    n = len(available)
    trending_count = sum(1 for r in available.values() if r == MarketRegime.TRENDING)
    sideways_count = sum(1 for r in available.values() if r == MarketRegime.SIDEWAYS)
    high_vol_count = sum(1 for r in available.values() if r == MarketRegime.HIGH_VOLATILITY)

    # Same precedence as classify_regime itself: high-volatility first
    # (a volatility shock is an override state regardless of whether
    # price also happens to be trending on some timeframes).
    if high_vol_count / n >= 0.5:
        regime, confidence, detail = _classify_high_vol_subtype(symbol, per_timeframe_indicators, high_vol_count, n)
    elif trending_count / n >= 0.5:
        regime, confidence, detail = _classify_trend_direction(per_timeframe_indicators, trending_count, n)
    elif sideways_count / n >= 0.5:
        regime, confidence, detail = _classify_sideways_subtype(per_timeframe_indicators, sideways_count, n)
    else:
        regime, confidence, detail = "UNDEFINED", 0.3, f"No regime holds a majority across {n} available timeframe(s)."

    return {
        "regime": regime, "confidence": round(confidence, 2), "detail": detail,
        "per_timeframe": per_timeframe, "unavailable_timeframes": unavailable,
    }


def _classify_trend_direction(per_timeframe_indicators: dict, trending_count: int, n: int) -> tuple[str, float, str]:
    """Direction from EMA9/EMA21 slope sign, voted only across the TRENDING-classified timeframes (a sideways timeframe's own slope is noise, not directional evidence)."""
    votes = []
    for tf, ind in per_timeframe_indicators.items():
        if classify_regime(ind) != MarketRegime.TRENDING:
            continue
        if ind["ema9_slope"] > 0 and ind["ema21_slope"] > 0:
            votes.append(1)
        elif ind["ema9_slope"] < 0 and ind["ema21_slope"] < 0:
            votes.append(-1)
        else:
            votes.append(0)

    bullish_votes = sum(1 for v in votes if v > 0)
    bearish_votes = sum(1 for v in votes if v < 0)

    if bullish_votes > bearish_votes:
        regime = "TRENDING_BULLISH"
    elif bearish_votes > bullish_votes:
        regime = "TRENDING_BEARISH"
    else:
        regime = "UNDEFINED"  # trending but slopes disagree/cancel out -- an honest "can't tell direction", not a guess
    detail = f"{trending_count}/{n} timeframes read TRENDING; {bullish_votes} bullish vs {bearish_votes} bearish slope votes among them."
    return regime, trending_count / n, detail


def _classify_sideways_subtype(per_timeframe_indicators: dict, sideways_count: int, n: int) -> tuple[str, float, str]:
    """LOW_VOLATILITY (very compressed) vs MEAN_REVERSION (keeps snapping between overbought/oversold) vs plain SIDEWAYS."""
    bb_widths = [ind["bb_width"] for ind in per_timeframe_indicators.values() if ind.get("bb_width") is not None]
    if bb_widths and (sum(bb_widths) / len(bb_widths)) < LOW_VOLATILITY_BB_WIDTH_THRESHOLD:
        return "LOW_VOLATILITY", sideways_count / n, f"{sideways_count}/{n} timeframes read SIDEWAYS with unusually compressed Bollinger width (avg {sum(bb_widths) / len(bb_widths):.4f})."

    rsi_values = [ind["rsi"] for ind in per_timeframe_indicators.values() if ind.get("rsi") is not None]
    extreme_readings = sum(1 for r in rsi_values if r >= RSI_EXTREME_HIGH or r <= RSI_EXTREME_LOW)
    if rsi_values and extreme_readings / len(rsi_values) >= 0.5:
        return "MEAN_REVERSION", sideways_count / n, f"{sideways_count}/{n} timeframes read SIDEWAYS, with RSI reaching overbought/oversold on {extreme_readings}/{len(rsi_values)} of them."

    return "SIDEWAYS", sideways_count / n, f"{sideways_count}/{n} timeframes read SIDEWAYS."


def _classify_high_vol_subtype(symbol: str, per_timeframe_indicators: dict, high_vol_count: int, n: int) -> tuple[str, float, str]:
    """BREAKOUT/BREAKDOWN (confirmed new-range price move alongside the vol expansion) vs EVENT_DRIVEN (extreme, synchronized, unconfirmed) vs plain HIGH_VOLATILITY."""
    from .models import HistoricalData

    reference_tf = "1d" if "1d" in per_timeframe_indicators else next(iter(per_timeframe_indicators))
    candles = list(HistoricalData.objects.filter(symbol=symbol, timeframe=reference_tf).order_by("-timestamp")[:20])

    if len(candles) >= 5:
        latest_close = float(candles[0].close)
        recent_high = max(float(c.high) for c in candles[1:])
        recent_low = min(float(c.low) for c in candles[1:])
        if latest_close >= recent_high:
            return "BREAKOUT", high_vol_count / n, f"{high_vol_count}/{n} timeframes read HIGH_VOLATILITY, and {reference_tf} close is at/above its recent {len(candles) - 1}-bar high."
        if latest_close <= recent_low:
            return "BREAKDOWN", high_vol_count / n, f"{high_vol_count}/{n} timeframes read HIGH_VOLATILITY, and {reference_tf} close is at/below its recent {len(candles) - 1}-bar low."

    bb_widths = [ind["bb_width"] for ind in per_timeframe_indicators.values() if ind.get("bb_width") is not None]
    all_extreme = bb_widths and high_vol_count == n and min(bb_widths) >= BB_WIDTH_HIGH_VOL_THRESHOLD * EVENT_DRIVEN_BB_WIDTH_MULTIPLE
    if all_extreme:
        return (
            "EVENT_DRIVEN", 0.3,
            f"UNCONFIRMED -- every available timeframe ({n}) reads HIGH_VOLATILITY with an extreme, synchronized "
            f"Bollinger-width expansion, no breakout/breakdown direction confirmed. This pattern often accompanies "
            f"a real news/event shock, but no event-calendar data exists in this platform to verify one is actually "
            f"happening (see apps.market_data.event_risk) -- treat this as low-confidence, not fact."
        )

    return "HIGH_VOLATILITY", high_vol_count / n, f"{high_vol_count}/{n} timeframes read HIGH_VOLATILITY, but price hasn't broken its recent range."
