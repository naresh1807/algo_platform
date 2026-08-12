"""
Computes the exact indicator set the manual's section 11 buy/sell logic
needs: EMA 9/21 (+ slopes), MACD + signal + histogram, RSI, Parabolic
SAR, ATR, ADX, Bollinger Band width, and relative volume.

IMPLEMENTATION NOTE: this used to call pandas_ta for all of the above.
pandas-ta 0.3.14b0 was last released around 2021 and is effectively
unmaintained -- beyond the numpy.NaN import bug already worked around
once, it was never updated against pandas' newer releases (pandas 3.0
shipped in 2026) or tested against newer Python versions (3.14, released
Oct 2025) at all. Rather than keep patching around an abandoned
dependency one bug at a time, every indicator below is now implemented
directly with plain pandas/numpy operations (.ewm(), .rolling(), basic
arithmetic) -- APIs that have been stable for many pandas major
versions and are actively maintained, so this module no longer depends
on pandas-ta at all. requirements.txt no longer lists it.

Kept as one function returning a plain dict (not a stateful class)
because apps.signals.engine needs a single snapshot of "indicators as of
the latest closed candle" -- there's no need to hold indicator state
between calls, and a pure function is trivial to unit test against a
fixed CSV of candles (see tests.py, which exercises this without any
pandas-ta dependency either).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .models import HistoricalData

MIN_CANDLES_REQUIRED = 60  # enough lookback for EMA21/ADX/BB to be meaningful, not just non-null


def load_candle_frame(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    """
    Pulls the most recent `limit` candles and returns them oldest-first
    (indicators need chronological order to compute correctly) as a
    DataFrame indexed by timestamp -- this is the one place raw
    HistoricalData rows get converted to the pandas shape every
    indicator function downstream expects.
    """
    rows = list(
        HistoricalData.objects.filter(symbol=symbol, timeframe=timeframe)
        .order_by("-timestamp")[:limit]
        .values("timestamp", "open", "high", "low", "close", "volume")
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows[::-1])  # reverse: DB gave newest-first, we need oldest-first
    df = df.set_index("timestamp")
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df


def _ema(series: pd.Series, length: int) -> pd.Series:
    """Standard exponential moving average, span-based smoothing (adjust=False matches the usual charting-platform convention)."""
    return series.ewm(span=length, adjust=False).mean()


def _wilder_smooth(series: pd.Series, length: int) -> pd.Series:
    """
    Wilder's smoothing (used for RSI, ATR, ADX/+DI/-DI in their
    original, standard definitions) -- equivalent to an EMA with
    alpha=1/length rather than the more common alpha=2/(length+1) a
    plain EMA uses. Using the wrong smoothing constant here would
    silently produce numbers that LOOK like RSI/ATR/ADX but don't match
    the standard formula any charting platform or the manual's own
    thresholds (e.g. ADX_TRENDING_THRESHOLD=25 in regime.py) were
    calibrated against.
    """
    return series.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = _wilder_smooth(gain, length)
    avg_loss = _wilder_smooth(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(100)  # avg_loss == 0 (all recent candles up) is maximal RSI, not undefined


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1,
    ).max(axis=1)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    return _wilder_smooth(_true_range(high, low, close), length)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index,
    )

    tr_smoothed = _wilder_smooth(_true_range(high, low, close), length)
    plus_di = 100 * (_wilder_smooth(plus_dm, length) / tr_smoothed)
    minus_di = 100 * (_wilder_smooth(minus_dm, length) / tr_smoothed)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder_smooth(dx.fillna(0), length)


def _bollinger_width(close: pd.Series, length: int = 20, num_std: float = 2.0) -> pd.Series:
    sma = close.rolling(length).mean()
    std = close.rolling(length).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    return (upper - lower) / sma


def _parabolic_sar(high: pd.Series, low: pd.Series, af_start: float = 0.02, af_step: float = 0.02, af_max: float = 0.2) -> pd.Series:
    """
    Standard iterative Wilder PSAR algorithm -- genuinely sequential
    (each value depends on the previous SAR, trend direction, extreme
    point, and acceleration factor), so unlike every other indicator
    here this cannot be vectorized into a single pandas expression;
    a plain Python loop over the (already-small, <=300-row) candle
    window is the correct and clear way to compute it.
    """
    n = len(high)
    sar = [0.0] * n
    if n == 0:
        return pd.Series(sar, index=high.index)

    trend_up = True
    af = af_start
    ep = high.iloc[0]
    sar[0] = low.iloc[0]

    for i in range(1, n):
        prev_sar = sar[i - 1]
        if trend_up:
            candidate = prev_sar + af * (ep - prev_sar)
            # SAR must never move into the price range of the last one
            # or two candles -- a well-known correction from the
            # standard algorithm, without which SAR can "jump" into
            # the middle of recent candles rather than trailing them.
            candidate = min(candidate, low.iloc[i - 1], low.iloc[i - 2] if i >= 2 else low.iloc[i - 1])
            if high.iloc[i] > ep:
                ep = high.iloc[i]
                af = min(af + af_step, af_max)
            if low.iloc[i] < candidate:
                trend_up = False
                candidate = ep
                ep = low.iloc[i]
                af = af_start
        else:
            candidate = prev_sar + af * (ep - prev_sar)
            candidate = max(candidate, high.iloc[i - 1], high.iloc[i - 2] if i >= 2 else high.iloc[i - 1])
            if low.iloc[i] < ep:
                ep = low.iloc[i]
                af = min(af + af_step, af_max)
            if high.iloc[i] > candidate:
                trend_up = True
                candidate = ep
                ep = high.iloc[i]
                af = af_start
        sar[i] = candidate

    return pd.Series(sar, index=high.index)


def _with_indicator_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Shared by compute_indicators() (single latest snapshot, used by the
    signal engine) and compute_indicator_series() (the whole frame, used
    by the dashboard chart) so both stay based on the exact same
    formulas -- a chart line and the value the signal engine acted on
    must never be able to drift apart into two slightly different
    "RSI"s.
    """
    df["ema9"] = _ema(df["close"], 9)
    df["ema21"] = _ema(df["close"], 21)

    ema12 = _ema(df["close"], 12)
    ema26 = _ema(df["close"], 26)
    df["macd"] = ema12 - ema26
    df["macd_signal"] = _ema(df["macd"], 9)
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    df["rsi"] = _rsi(df["close"], 14)
    df["atr"] = _atr(df["high"], df["low"], df["close"], 14)
    df["adx"] = _adx(df["high"], df["low"], df["close"], 14)
    df["bb_width"] = _bollinger_width(df["close"], 20, 2.0)
    df["sar"] = _parabolic_sar(df["high"], df["low"])
    df["vol_avg_20"] = df["volume"].rolling(20).mean()
    return df


def load_full_indicator_frame(symbol: str, timeframe: str, limit: int = 5000) -> pd.DataFrame:
    """
    Public counterpart to compute_indicators()/compute_indicator_series()
    for callers that need every indicator column across the WHOLE
    history at once (not just the latest snapshot, and not just the
    chart-panel subset compute_indicator_series returns) -- currently
    only apps.analytics.backtest, which needs to replay
    apps.signals.engine's exact technical-condition logic bar-by-bar
    against real historical candles. Reuses _with_indicator_columns
    (the same private helper compute_indicators() itself calls) so a
    backtested RSI/MACD/etc. can never silently drift from what the
    live signal engine actually saw -- same "one formula, not two"
    principle _with_indicator_columns' own docstring already states.

    Returns an empty DataFrame (not raising) below MIN_CANDLES_REQUIRED
    rows, same "not enough data is normal" stance as the rest of this
    module. limit defaults far higher than compute_indicators()'s 300
    (a chart snapshot) since a meaningful backtest needs as much
    history as is actually stored.
    """
    df = load_candle_frame(symbol, timeframe, limit=limit)
    if len(df) < MIN_CANDLES_REQUIRED:
        return pd.DataFrame()
    return _with_indicator_columns(df)


def indicator_dict_at(df: pd.DataFrame, i: int) -> dict:
    """
    Builds the exact same dict shape compute_indicators() returns for
    "latest," but for row `i` of an already-indicator-enriched
    DataFrame (from load_full_indicator_frame) -- lets a backtest call
    the identical apps.signals.engine condition functions
    (_evaluate_buy_conditions, classify_regime) bar-by-bar without that
    engine code needing to know or care whether it's looking at "the
    live latest candle" or "candle #4,102 of a historical replay."
    Requires i >= 1 (needs a previous row for slopes/macd_hist_prev),
    same implicit requirement compute_indicators() has via df.iloc[-2].
    """
    latest = df.iloc[i]
    prev = df.iloc[i - 1]
    window = df.iloc[max(0, i - 1):i + 1]
    return {
        "close": float(latest["close"]),
        "ema9": float(latest["ema9"]),
        "ema21": float(latest["ema21"]),
        "ema9_slope": float(latest["ema9"] - prev["ema9"]),
        "ema21_slope": float(latest["ema21"] - prev["ema21"]),
        "macd": float(latest["macd"]),
        "macd_signal": float(latest["macd_signal"]),
        "macd_hist": float(latest["macd_hist"]),
        "macd_hist_prev": float(prev["macd_hist"]),
        "rsi": float(latest["rsi"]),
        "sar": float(latest["sar"]),
        "atr": float(latest["atr"]),
        "adx": float(latest["adx"]),
        "bb_width": float(latest["bb_width"]),
        "relative_volume": (
            float(latest["volume"] / latest["vol_avg_20"]) if latest["vol_avg_20"] else 1.0
        ),
        "close_below_ema9_streak": _count_close_below_ema9(window),
        "close_above_ema9_streak": _count_close_above_ema9(window),
    }


def compute_indicators(symbol: str, timeframe: str) -> dict | None:
    """
    Returns None (not an exception) when there isn't enough history yet
    -- apps.signals.engine treats "not enough data" as a NO_TRADE
    decision with a clear reason, not a crash, since a fresh symbol
    with only a few candles is an expected, ordinary state early on.
    """
    df = load_candle_frame(symbol, timeframe)
    if len(df) < MIN_CANDLES_REQUIRED:
        return None

    df = _with_indicator_columns(df)

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    return {
        "close": float(latest["close"]),
        "ema9": float(latest["ema9"]),
        "ema21": float(latest["ema21"]),
        "ema9_slope": float(latest["ema9"] - prev["ema9"]),
        "ema21_slope": float(latest["ema21"] - prev["ema21"]),
        "macd": float(latest["macd"]),
        "macd_signal": float(latest["macd_signal"]),
        "macd_hist": float(latest["macd_hist"]),
        "macd_hist_prev": float(prev["macd_hist"]),
        "rsi": float(latest["rsi"]),
        "sar": float(latest["sar"]),
        "atr": float(latest["atr"]),
        "adx": float(latest["adx"]),
        "bb_width": float(latest["bb_width"]),
        "relative_volume": (
            float(latest["volume"] / latest["vol_avg_20"]) if latest["vol_avg_20"] else 1.0
        ),
        "close_below_ema9_streak": _count_close_below_ema9(df),
        "close_above_ema9_streak": _count_close_above_ema9(df),
    }


def compute_indicator_series(symbol: str, timeframe: str, limit: int = 300) -> list[dict]:
    """
    Row-per-candle indicator values (unlike compute_indicators(), which
    only returns the latest snapshot for the signal engine). This is
    what apps.market_data.views.IndicatorSeriesView serves to the
    dashboard so PriceChart can draw EMA9/EMA21/SAR as overlay lines
    and RSI/MACD as separate panel series -- exactly like a normal
    charting platform, instead of the chart only ever showing candles.

    Returns [] rather than raising when there's too little history,
    same "not enough data is normal, not an error" stance as
    compute_indicators().
    """
    df = load_candle_frame(symbol, timeframe, limit=limit)
    if len(df) < MIN_CANDLES_REQUIRED:
        return []

    df = _with_indicator_columns(df)

    out = []
    for ts, row in df.iterrows():
        out.append({
            "timestamp": ts.isoformat(),
            "ema9": None if pd.isna(row["ema9"]) else float(row["ema9"]),
            "ema21": None if pd.isna(row["ema21"]) else float(row["ema21"]),
            "sar": None if pd.isna(row["sar"]) else float(row["sar"]),
            "rsi": None if pd.isna(row["rsi"]) else float(row["rsi"]),
            "macd": None if pd.isna(row["macd"]) else float(row["macd"]),
            "macd_signal": None if pd.isna(row["macd_signal"]) else float(row["macd_signal"]),
            "macd_hist": None if pd.isna(row["macd_hist"]) else float(row["macd_hist"]),
        })
    return out


def _count_close_below_ema9(df: pd.DataFrame, lookback: int = 2) -> int:
    """
    Powers the exit condition "Close below EMA 9 for 2 candles" (section
    11) -- counts how many of the most recent `lookback` candles closed
    below EMA9, so the signal engine can check `>= 2` directly.
    """
    recent = df.tail(lookback)
    return int((recent["close"] < recent["ema9"]).sum())


def _count_close_above_ema9(df: pd.DataFrame, lookback: int = 2) -> int:
    """
    Mirror of _count_close_below_ema9 -- powers a SHORT position's exit
    condition (apps.signals.engine._evaluate_exit_conditions_short):
    "close back above EMA9 for 2 candles" is the bullish-reversal
    equivalent of the LONG exit's bearish-breakdown signal.
    """
    recent = df.tail(lookback)
    return int((recent["close"] > recent["ema9"]).sum())
