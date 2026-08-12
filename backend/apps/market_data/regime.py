"""
Classifies the current regime as trending / sideways / high_volatility
using the exact inputs the manual's section 12 lists: ADX, ATR
expansion, EMA slope, and Bollinger width. News-shock intensity (the
5th input listed in the manual) is deliberately NOT computed here --
it depends on apps.news sentiment data, and pulling that in would make
this module depend on apps.news. Instead, apps.signals.engine combines
this module's regime classification with a separate "news shock" check
from apps.news when deciding whether to pause entries -- keeping
market_data's regime detection purely price-based and reusable on its
own.

(Correlated-exposure checking, previously a documented gap here, now
lives in apps/market_data/correlation.py + apps/risk/engine.py's
_check_correlated_exposure -- kept as a separate module from regime
detection since correlation is about relationships BETWEEN symbols,
not a single symbol's own price behavior.)
"""

from __future__ import annotations

from common.constants import MarketRegime

# Thresholds are intentionally named constants, not inline magic
# numbers, so a future tuning pass (part of the daily learning loop,
# manual section 15 -- "soft parameters" only) has one obvious place to
# adjust them, and code reading this function doesn't need to guess
# why "25" or "0.04" appear.
ADX_TRENDING_THRESHOLD = 25.0
BB_WIDTH_HIGH_VOL_THRESHOLD = 0.06  # Bollinger band width as a fraction of price
ATR_EXPANSION_RATIO_HIGH_VOL = 1.5  # today's ATR vs. its own recent average


def classify_regime(indicators: dict, atr_baseline: float | None = None) -> str:
    """
    indicators: the dict returned by apps.market_data.indicators.compute_indicators.
    atr_baseline: a longer-lookback average ATR to compare the current
    ATR against for "expansion" -- optional because early on (not much
    history yet) there may not be enough data to compute a stable
    baseline; falls back to ADX/BB-width-only classification in that case.

    Precedence order matters here: high-volatility is checked FIRST and
    wins over trending, because the manual treats a volatility/event
    shock as an override state ("stricter filters and smaller size")
    regardless of whether price also happens to be trending -- a
    strong trend during a volatility spike should still get the more
    cautious treatment.
    """
    adx = indicators["adx"]
    bb_width = indicators["bb_width"]
    atr = indicators["atr"]

    atr_expanded = (
        atr_baseline is not None
        and atr_baseline > 0
        and (atr / atr_baseline) >= ATR_EXPANSION_RATIO_HIGH_VOL
    )

    if bb_width >= BB_WIDTH_HIGH_VOL_THRESHOLD or atr_expanded:
        return MarketRegime.HIGH_VOLATILITY

    if adx >= ADX_TRENDING_THRESHOLD:
        return MarketRegime.TRENDING

    return MarketRegime.SIDEWAYS


def regime_size_multiplier(regime: str) -> float:
    """
    manual section 12, "Behavior": trending=normal size, sideways=smaller
    size (or skip), high-volatility=smaller size. Returned as a
    multiplier apps.signals.engine applies on top of whatever position
    size apps.risk.engine computed -- regime adjusts size, it never
    overrides the hard risk limits themselves.
    """
    return {
        MarketRegime.TRENDING: 1.0,
        MarketRegime.SIDEWAYS: 0.5,
        MarketRegime.HIGH_VOLATILITY: 0.5,
    }.get(regime, 0.5)
