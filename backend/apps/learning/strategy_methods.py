"""
Three genuinely different signal-idea generators for
apps.learning.tasks.run_strategy_method_comparison -- NOT variations on
apps.signals.engine's own parameters (that's what StrategyVersion.
params_json already does), but different trading logic entirely, so
their paper-trade outcomes are a real comparison of approaches rather
than the same approach with different knobs.

Each function is PURE and READ-ONLY: it calls
apps.market_data.indicators.compute_indicators/load_candle_frame and
apps.market_data.regime.classify_regime (the same real, shared
indicator math apps.signals.engine itself uses, so a method's "RSI" or
"ADX" can never silently drift from what the live chart/engine sees)
and returns either None (no trade this cycle) or a plain dict of
{"entry_price", "stop_loss", "target_price"}. No DB writes happen
here -- see apps/learning/models.py's HypotheticalTrade docstring for
why persistence is kept in the calling task instead, and why this
never touches apps.execution/apps.risk/real money.
"""

from __future__ import annotations

from apps.market_data.indicators import compute_indicators, load_candle_frame
from apps.market_data.regime import classify_regime

# How many prior bars (excluding the current one) define "the recent
# range" for the breakout method's compression/breakout check.
BREAKOUT_LOOKBACK_BARS = 20
BREAKOUT_VOLUME_RATIO = 1.5
BREAKOUT_BB_WIDTH_SQUEEZE = 0.03  # tighter than regime.py's own 0.06 high-vol threshold -- this is looking for a QUIET range about to break out, the opposite condition

MEAN_REVERSION_RSI_OVERSOLD = 30.0


def generate_trend_following_idea(symbol: str, timeframe: str = "5m") -> dict | None:
    """
    Classic trend-following: only enter when the trend is both
    STRUCTURALLY established (EMA9 above EMA21, i.e. the fast average
    already leads the slow one) and CURRENTLY STRENGTHENING (EMA9 still
    rising) and CONFIRMED by ADX -- the same ADX>=25 threshold
    apps.market_data.regime.classify_regime itself uses for TRENDING,
    reused rather than re-guessed. Wide stop (1.5x ATR) and a 2R target
    -- trend trades are given room to develop, matching how trend-
    following is actually traded (few, larger winners, not scalped).
    """
    ind = compute_indicators(symbol, timeframe)
    if ind is None:
        return None
    regime = classify_regime(ind)

    is_uptrend = ind["ema9"] > ind["ema21"] and ind["ema9_slope"] > 0
    is_confirmed = ind["adx"] >= 25.0
    if regime == "high_volatility" or not (is_uptrend and is_confirmed):
        return None

    entry = ind["close"]
    stop = entry - 1.5 * ind["atr"]
    risk = entry - stop
    if risk <= 0:
        return None
    return {"entry_price": entry, "stop_loss": stop, "target_price": entry + 2.0 * risk}


def generate_mean_reversion_idea(symbol: str, timeframe: str = "5m") -> dict | None:
    """
    Bets on a bounce, the opposite read of price action from trend-
    following: price is OVERSOLD (RSI<=30) and stretched below its own
    trend average (close below EMA21) -- not "the trend is down," but
    "this move down looks overdone." Tighter stop (1.0x ATR, mean-
    reversion trades are meant to resolve quickly) and a smaller 1.5R
    target -- taking profit on the bounce, not riding a new trend.
    Skipped in HIGH_VOLATILITY regime -- an "oversold" reading during a
    volatility shock is exactly when mean reversion is most likely to
    keep falling instead of bouncing.
    """
    ind = compute_indicators(symbol, timeframe)
    if ind is None:
        return None
    regime = classify_regime(ind)

    is_oversold = ind["rsi"] <= MEAN_REVERSION_RSI_OVERSOLD
    is_stretched = ind["close"] < ind["ema21"]
    if regime == "high_volatility" or not (is_oversold and is_stretched):
        return None

    entry = ind["close"]
    stop = entry - 1.0 * ind["atr"]
    risk = entry - stop
    if risk <= 0:
        return None
    return {"entry_price": entry, "stop_loss": stop, "target_price": entry + 1.5 * risk}


def generate_breakout_idea(symbol: str, timeframe: str = "5m") -> dict | None:
    """
    Looks for a QUIET range (Bollinger width compressed, below
    BREAKOUT_BB_WIDTH_SQUEEZE -- the opposite condition from regime.py's
    high-volatility threshold) suddenly resolving with a volume spike
    (relative_volume >= 1.5) and price closing above its own recent
    (BREAKOUT_LOOKBACK_BARS-bar) high -- compression-then-expansion is
    the classic breakout setup, distinct from both trend-following
    (already-established, confirmed trend) and mean-reversion (betting
    against the recent move). Widest target (2.5R) since a genuine
    breakout from compression is expected to run further than either
    of the other two methods' setups.
    """
    ind = compute_indicators(symbol, timeframe)
    if ind is None:
        return None

    is_squeeze = ind["bb_width"] <= BREAKOUT_BB_WIDTH_SQUEEZE
    is_volume_spike = ind["relative_volume"] >= BREAKOUT_VOLUME_RATIO
    if not (is_squeeze and is_volume_spike):
        return None

    df = load_candle_frame(symbol, timeframe, limit=BREAKOUT_LOOKBACK_BARS + 1)
    if len(df) < BREAKOUT_LOOKBACK_BARS + 1:
        return None
    prior_high = float(df["high"].iloc[:-1].max())  # excludes the current (last) bar -- comparing against bars BEFORE this one, not including it
    if ind["close"] <= prior_high:
        return None

    entry = ind["close"]
    stop = entry - 1.5 * ind["atr"]
    risk = entry - stop
    if risk <= 0:
        return None
    return {"entry_price": entry, "stop_loss": stop, "target_price": entry + 2.5 * risk}


METHOD_FUNCS = {
    "trend_following": generate_trend_following_idea,
    "mean_reversion": generate_mean_reversion_idea,
    "breakout": generate_breakout_idea,
}
