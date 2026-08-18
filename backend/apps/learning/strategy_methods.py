"""
Six genuinely different signal-idea generators, in two groups -- NOT
variations on apps.signals.engine's own parameters (that's what
StrategyVersion.params_json already does), but different trading logic
entirely, so their paper-trade outcomes are a real comparison of
approaches rather than the same approach with different knobs:

  METHOD_FUNCS (5m, apps.learning.tasks.run_strategy_method_comparison):
    trend-following / mean-reversion / breakout -- wider stops/targets,
    meant to develop over several bars.

  SCALPING_METHOD_FUNCS (1m, apps.learning.tasks.
  run_scalping_strategy_comparison): ema-momentum / rsi-extreme /
  sar-volume-burst -- tight stops, quick 1-1.5R targets, a much shorter
  max holding period. Genuinely faster/tighter setups, not the same
  three ideas just run on a shorter candle.

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

The three SCALPING_METHOD_FUNCS also include the full "ind" dict they
computed internally in their returned dict (the three swing METHOD_FUNCS
do not) -- apps.learning.tasks._run_comparison_cycle pops it out and
converts it to HypotheticalTrade's ind_* columns, which feed
apps.learning.scalp_ml_train's win-probability model. Not added to the
three swing functions too since no model consumes their data yet (see
scalp_ml_train's own docstring for why that model is scoped to scalping
only) -- extend them the same way if that ever changes.
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

# --- Scalping methods (1m timeframe, see SCALPING_METHOD_FUNCS below) ---
# Deliberately tighter/faster than the three swing methods above: small
# ATR-based (or SAR-based) stops and quick 1-1.5R targets, matching how
# scalping is actually traded (many small, fast wins; cut losers
# immediately) rather than the wider stops/targets a 5m swing idea uses.
SCALPING_RSI_EXTREME_OVERSOLD = 20.0  # more extreme than MEAN_REVERSION_RSI_OVERSOLD -- scalping wants a sharper snap-back, not just "a bit oversold"
SCALPING_VOLUME_SPIKE_RATIO = 1.5


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
    # None (not a number) means no real volume signal exists for this
    # symbol at all (see apps.market_data.indicators' relative_volume
    # docstring -- NIFTY/BANKNIFTY report volume=0 on every candle), so
    # treat it as "can't check, don't block on it" rather than a
    # structurally-permanent False -- the latter made this method
    # return None on every single call for these two symbols.
    is_volume_spike = ind["relative_volume"] is None or ind["relative_volume"] >= BREAKOUT_VOLUME_RATIO
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


def generate_ema_momentum_scalp_idea(symbol: str, timeframe: str = "1m") -> dict | None:
    """
    Fast-timing momentum scalp: EMA9 already above EMA21 (structural
    uptrend, same check trend_following uses) but entered on the exact
    bar momentum visibly KICKS IN (MACD histogram just turned positive
    AND is rising) rather than trend_following's "already confirmed by
    ADX>=25" bar -- scalping cares about catching the move AS it starts,
    not waiting for a slower confirmation that would mean entering late
    on a 1-minute chart. relative_volume confirms real participation,
    not just noise. 0.6x ATR stop / 1R target -- small and fast.
    """
    ind = compute_indicators(symbol, timeframe)
    if ind is None:
        return None

    is_uptrend = ind["ema9"] > ind["ema21"]
    is_momentum_turning = ind["macd_hist"] > 0 and ind["macd_hist"] > ind["macd_hist_prev"]
    # None-means-unavailable-skip: see generate_breakout_idea's note.
    is_volume_confirmed = ind["relative_volume"] is None or ind["relative_volume"] >= 1.2
    if not (is_uptrend and is_momentum_turning and is_volume_confirmed):
        return None

    entry = ind["close"]
    stop = entry - 0.6 * ind["atr"]
    risk = entry - stop
    if risk <= 0:
        return None
    return {"entry_price": entry, "stop_loss": stop, "target_price": entry + 1.0 * risk, "ind": ind}


def generate_rsi_extreme_scalp_idea(symbol: str, timeframe: str = "1m") -> dict | None:
    """
    Sharper, faster mean-reversion than generate_mean_reversion_idea:
    RSI<=SCALPING_RSI_EXTREME_OVERSOLD (20, vs. that method's 30) --
    scalping wants a genuinely stretched, snap-back-prone reading, not
    just "leaning oversold," since the target is a quick 1.2R bounce,
    not a sustained reversal. Same HIGH_VOLATILITY skip as
    generate_mean_reversion_idea and for the same reason (an extreme
    reading during a volatility shock is more likely to keep falling).
    """
    ind = compute_indicators(symbol, timeframe)
    if ind is None:
        return None
    regime = classify_regime(ind)
    if regime == "high_volatility" or ind["rsi"] > SCALPING_RSI_EXTREME_OVERSOLD:
        return None

    entry = ind["close"]
    stop = entry - 0.5 * ind["atr"]
    risk = entry - stop
    if risk <= 0:
        return None
    return {"entry_price": entry, "stop_loss": stop, "target_price": entry + 1.2 * risk, "ind": ind}


def generate_sar_volume_burst_scalp_idea(symbol: str, timeframe: str = "1m") -> dict | None:
    """
    Catches a fresh directional burst right as it starts: Parabolic SAR
    just below price (a bullish flip/continuation) combined with a real
    volume spike (relative_volume >= 1.5, tighter bar than the other two
    scalp methods' 1.2 -- a burst needs a genuine spike, not just
    "slightly above average"). Stop is placed AT the SAR value itself
    (not an ATR multiple) -- SAR's own point IS the level that
    invalidates the setup, the tightest, most precise stop of the three
    scalp methods, matching how SAR is actually used for stop placement
    in practice.
    """
    ind = compute_indicators(symbol, timeframe)
    if ind is None:
        return None

    is_sar_bullish = ind["sar"] < ind["close"]
    # None-means-unavailable-skip: see generate_breakout_idea's note.
    is_volume_burst = ind["relative_volume"] is None or ind["relative_volume"] >= SCALPING_VOLUME_SPIKE_RATIO
    if not (is_sar_bullish and is_volume_burst):
        return None

    entry = ind["close"]
    stop = ind["sar"]
    risk = entry - stop
    if risk <= 0:
        return None
    return {"entry_price": entry, "stop_loss": stop, "target_price": entry + 1.5 * risk, "ind": ind}


METHOD_FUNCS = {
    "trend_following": generate_trend_following_idea,
    "mean_reversion": generate_mean_reversion_idea,
    "breakout": generate_breakout_idea,
}

# Run separately from METHOD_FUNCS (apps.learning.tasks.
# run_scalping_strategy_comparison, its own 1-minute beat schedule
# entry, its own shorter max-holding-bars) -- these are genuinely
# faster/tighter setups meant for the 1m timeframe, not just the same
# three ideas run more often.
SCALPING_METHOD_FUNCS = {
    "ema_momentum_scalp": generate_ema_momentum_scalp_idea,
    "rsi_extreme_scalp": generate_rsi_extreme_scalp_idea,
    "sar_volume_burst_scalp": generate_sar_volume_burst_scalp_idea,
}
