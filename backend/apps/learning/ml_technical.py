"""
Technical-direction ML model -- learns directly from candle structure
and the FULL indicator suite (apps.market_data.indicators), trained on
real historical HistoricalData rows, not trade outcomes.

This is DELIBERATELY SEPARATE from ml_train.py's win-probability model:
  - ml_train.py learns "given a signal the rule engine already approved,
    how often did setups like it actually win" -- its training set is
    closed trades, which start at zero and accumulate slowly (one row
    per real paper/live trade close).
  - ml_technical.py (this module) learns "given the full technical
    state at ANY bar, was a hypothetical 1R-target/1R-stop long entry
    there favorable" -- its training set is every historical candle
    that has enough forward bars to know the answer, which for this
    platform's ~50k real backfilled candles means tens of thousands of
    labeled rows available immediately, no waiting for live trading.

Both are honest about what they are: this model does NOT know whether
the rule-based engine would have even generated a signal at a given
bar (regime filters, sentiment, options confluence, risk-engine state
are not part of its label or features) -- it answers a narrower
question ("did the technical picture alone predict a favorable
1R:1R outcome") that ml_train's model cannot yet answer for lack of
data, and that apps.analytics.backtest only answers for the rule
engine's specific fixed conditions, not the full continuous indicator
space. Treat its output as one more input to weigh, not a verdict.

STATISTICAL HONESTY NOTE on sample size: row_count/train_size/holdout_size
count BARS, not independent trials -- consecutive starting bars' forward
-looking outcome windows (max_holding_bars long) overlap heavily, so the
effective independent sample size is meaningfully smaller than the raw
row count suggests (roughly row_count / max_holding_bars as a rough
floor, not row_count itself). This is exactly why this module's results
should be cross-checked against apps.analytics.backtest's independently
-computed results (different method, different sample construction)
rather than trusted alone -- agreement between the two (as found for
BANKNIFTY 15m and NIFTY 5m on 2026-08-08) is a much stronger signal than
either method's holdout metrics in isolation. Where they disagree (1h
timeframe results diverged between the two methods that day), treat
BOTH results with extra caution rather than picking whichever looked
better.

Higher-capacity model choice (HistGradientBoostingClassifier) is
justified here specifically because the label-generation approach
above produces thousands of rows per symbol/timeframe -- squarely past
the "few hundred+ trades" bar ml_train.py's own docstring sets for when
a bigger model stops being "mostly learning noise." Logistic regression
remains the right choice for ml_train.py's own much smaller trade-count
dataset; the model choice is a property of the dataset size, not a
blanket "bigger is better."
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Same walk-forward / chronological-split conventions as ml_train.py and
# apps.analytics.backtest -- a random split would let the model "see the
# future," inflating reported performance versus live use.
DEFAULT_ATR_MULTIPLIER = 1.5
DEFAULT_MAX_HOLDING_BARS = 48
DEFAULT_TEST_FRACTION = 0.25
MIN_ROWS_REQUIRED = 300  # below this, a holdout would be too thin to mean anything


def _simulate_labels(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, atr: np.ndarray,
    atr_multiplier: float, max_holding_bars: int,
) -> tuple[np.ndarray, np.ndarray]:
    """
    For every bar i (except the last few, which don't have enough
    forward bars left), simulates a hypothetical long entry at close[i]
    with stop = close[i] - atr[i]*atr_multiplier and a 1R target
    (entry + (entry - stop)) -- EXACT same convention as
    apps.analytics.backtest._simulate_exit (stop-loss checked before
    target on any bar where both could theoretically have been hit,
    the conservative assumption absent intra-candle tick data).

    Returns (labels, r_multiples), both float arrays with NaN where a
    label couldn't be computed (invalid/zero ATR, or too close to the
    end of the series to know the outcome yet) -- NaN rows are dropped
    by the caller, not treated as a class.

    Implemented with plain numpy array indexing (not pandas .iloc in a
    loop) specifically for speed -- this runs an O(n * max_holding_bars)
    scan, and with n in the tens of thousands, pandas' per-access
    overhead would make this noticeably slow for what is otherwise a
    simple array walk.
    """
    n = len(close)
    labels = np.full(n, np.nan)
    r_multiples = np.full(n, np.nan)

    for i in range(n - 1):
        entry = close[i]
        a = atr[i]
        if np.isnan(a) or a <= 0:
            continue
        stop = entry - a * atr_multiplier
        if stop >= entry:
            continue
        target = entry + (entry - stop)

        last_index = min(i + max_holding_bars, n - 1)
        outcome = None
        for j in range(i + 1, last_index + 1):
            if low[j] <= stop:
                outcome = -1.0
                break
            if high[j] >= target:
                outcome = 1.0
                break
        if outcome is None:
            if last_index <= i:
                continue
            risk = entry - stop
            outcome = (close[last_index] - entry) / risk if risk > 0 else 0.0

        r_multiples[i] = outcome
        labels[i] = 1.0 if outcome > 0 else 0.0

    return labels, r_multiples


def _classify_regime_series(df: pd.DataFrame) -> pd.Series:
    """
    Vectorized re-statement of apps.market_data.regime.classify_regime's
    exact rule (high_volatility precedence over trending over sideways)
    -- reimplemented here rather than calling that function per-row
    because it takes a single indicator dict, not a DataFrame, and an
    atr_baseline (a rolling mean of ATR) computed once for the whole
    series is both faster and MORE correct than the live per-call
    version (which often runs with atr_baseline=None early in a
    symbol's history and falls back to ADX/BB-width-only). Thresholds
    (ADX_TRENDING_THRESHOLD, BB_WIDTH_HIGH_VOL_THRESHOLD,
    ATR_EXPANSION_RATIO_HIGH_VOL) are imported from that module directly
    so the two can never silently drift apart.
    """
    from apps.market_data.regime import (
        ADX_TRENDING_THRESHOLD, ATR_EXPANSION_RATIO_HIGH_VOL, BB_WIDTH_HIGH_VOL_THRESHOLD,
    )
    from common.constants import MarketRegime

    atr_baseline = df["atr"].rolling(50, min_periods=10).mean()
    atr_expanded = (atr_baseline > 0) & ((df["atr"] / atr_baseline) >= ATR_EXPANSION_RATIO_HIGH_VOL)
    high_vol = (df["bb_width"] >= BB_WIDTH_HIGH_VOL_THRESHOLD) | atr_expanded
    trending = (~high_vol) & (df["adx"] >= ADX_TRENDING_THRESHOLD)

    regime = pd.Series(MarketRegime.SIDEWAYS, index=df.index)
    regime[trending] = MarketRegime.TRENDING
    regime[high_vol] = MarketRegime.HIGH_VOLATILITY
    return regime


# Fixed, explicit feature list -- same "a future new column doesn't
# silently change an already-trained model's expected input shape"
# reasoning as ml_features.BASE_FEATURES. All price-scale values are
# expressed as ratios/percentages of `close` (never raw price levels),
# so one model can generalize across symbols whose price levels differ
# by orders of magnitude (NIFTY ~25,000 vs BANKNIFTY ~55,000).
TECHNICAL_FEATURES = [
    "price_vs_ema9_pct", "price_vs_ema21_pct", "ema9_vs_ema21_pct",
    "ema9_slope_pct", "ema21_slope_pct",
    "macd_pct", "macd_signal_pct", "macd_hist_pct", "macd_hist_prev_pct", "macd_hist_rising",
    "rsi_norm", "adx_norm", "bb_width", "atr_pct", "relative_volume",
    "close_below_ema9_streak",
    "body_pct", "upper_wick_pct", "lower_wick_pct", "range_pct", "candle_bullish",
    "return_1", "return_3", "return_5", "return_10", "realized_vol_10",
    "regime_trending", "regime_sideways", "regime_high_volatility",
]


def _build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """
    df: output of apps.market_data.indicators.load_full_indicator_frame
    (already has ema9/ema21/macd/macd_signal/macd_hist/rsi/atr/adx/
    bb_width/sar/vol_avg_20 columns). Returns a NEW frame with exactly
    TECHNICAL_FEATURES as columns, same index (timestamp) as the input,
    so labels built separately can be joined back on.
    """
    close = df["close"]
    out = pd.DataFrame(index=df.index)

    out["price_vs_ema9_pct"] = (close - df["ema9"]) / close
    out["price_vs_ema21_pct"] = (close - df["ema21"]) / close
    out["ema9_vs_ema21_pct"] = (df["ema9"] - df["ema21"]) / close
    out["ema9_slope_pct"] = df["ema9"].diff() / close
    out["ema21_slope_pct"] = df["ema21"].diff() / close

    out["macd_pct"] = df["macd"] / close
    out["macd_signal_pct"] = df["macd_signal"] / close
    out["macd_hist_pct"] = df["macd_hist"] / close
    out["macd_hist_prev_pct"] = df["macd_hist"].shift(1) / close
    out["macd_hist_rising"] = (df["macd_hist"] > df["macd_hist"].shift(1)).astype(float)

    out["rsi_norm"] = df["rsi"] / 100.0
    out["adx_norm"] = df["adx"] / 100.0
    out["bb_width"] = df["bb_width"]
    out["atr_pct"] = df["atr"] / close
    # Index symbols (NIFTY/BANKNIFTY) report volume=0 from Angel One --
    # matches apps.market_data.indicators.compute_indicators/
    # indicator_dict_at's own "no volume data -> neutral 1.0, not NaN or
    # a divide-by-zero" fallback exactly, so a 0-volume index doesn't
    # nuke this feature (and therefore every row, via dropna()) to NaN.
    out["relative_volume"] = np.where(
        df["vol_avg_20"] > 0, df["volume"] / df["vol_avg_20"].replace(0, np.nan), 1.0,
    )
    out["close_below_ema9_streak"] = (close < df["ema9"]).rolling(2, min_periods=1).sum()

    body = close - df["open"]
    candle_range = (df["high"] - df["low"]).replace(0, np.nan)
    out["body_pct"] = body / close
    out["upper_wick_pct"] = (df["high"] - df[["open", "close"]].max(axis=1)) / close
    out["lower_wick_pct"] = (df[["open", "close"]].min(axis=1) - df["low"]) / close
    out["range_pct"] = (df["high"] - df["low"]) / close
    out["candle_bullish"] = (close > df["open"]).astype(float)

    returns_1 = close.pct_change(1)
    out["return_1"] = returns_1
    out["return_3"] = close.pct_change(3)
    out["return_5"] = close.pct_change(5)
    out["return_10"] = close.pct_change(10)
    out["realized_vol_10"] = returns_1.rolling(10, min_periods=5).std()

    regime = _classify_regime_series(df)
    from common.constants import MarketRegime
    out["regime_trending"] = (regime == MarketRegime.TRENDING).astype(float)
    out["regime_sideways"] = (regime == MarketRegime.SIDEWAYS).astype(float)
    out["regime_high_volatility"] = (regime == MarketRegime.HIGH_VOLATILITY).astype(float)

    return out[TECHNICAL_FEATURES]


def _fit_and_evaluate(X: np.ndarray, y: np.ndarray, feature_names: list[str]) -> tuple:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

    n = len(y)
    split = int(n * (1 - DEFAULT_TEST_FRACTION))
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    def _new_model():
        return HistGradientBoostingClassifier(
            max_iter=200, max_depth=6, learning_rate=0.08,
            l2_regularization=1.0, random_state=42,
        )

    eval_model = _new_model()
    eval_model.fit(X_train, y_train)
    preds = eval_model.predict(X_test)
    probs = eval_model.predict_proba(X_test)[:, 1]

    metrics = {
        "eval_type": "chronological_holdout",
        "train_size": len(y_train), "holdout_size": len(y_test),
        "accuracy": round(float(accuracy_score(y_test, preds)), 4),
        "brier_score": round(float(brier_score_loss(y_test, probs)), 4),
        "baseline_win_rate_holdout": round(float(y_test.mean()), 4),
    }
    if len(set(y_test)) == 2:
        metrics["roc_auc"] = round(float(roc_auc_score(y_test, probs)), 4)

    # HistGradientBoostingClassifier has no built-in .feature_importances_
    # (unlike RandomForest/GradientBoostingClassifier) -- permutation
    # importance (computed on the held-out slice, not train data, so it
    # reflects genuine predictive contribution rather than in-sample
    # overfitting) is the standard substitute, same "AI must explain
    # every signal" principle ml_train.py's coefficient reporting serves.
    try:
        perm = permutation_importance(
            eval_model, X_test, y_test, n_repeats=5, random_state=42, scoring="roc_auc",
        )
        metrics["feature_importances"] = dict(
            zip(feature_names, np.round(perm.importances_mean, 4).tolist())
        )
    except Exception:
        logger.exception("permutation_importance failed -- continuing without feature_importances.")

    # Deployed artifact refit on ALL data, same reasoning as ml_train.py:
    # the holdout split exists only to get an honest metric, not to
    # throw away the most recent (most relevant) slice of data.
    final_model = _new_model()
    final_model.fit(X, y)
    return final_model, metrics


def train_technical_direction_model(
    symbol: str, timeframe: str,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
    max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
) -> dict:
    """
    Trains (and, if it passes the same champion/challenger promotion
    logic ml_train.py uses, activates) a technical-direction model for
    one symbol+timeframe. Registered in the SAME ModelRegistry table as
    the win-probability model, under model_name=
    f"technical_direction_{symbol}_{timeframe}" -- a distinct model per
    symbol+timeframe (not combined with a symbol one-hot the way
    win-probability is) because backtesting already showed materially
    different dynamics per symbol/timeframe (see apps.analytics.backtest
    results from 2026-08-08: NIFTY 5m showed no real edge while
    NIFTY 1h did) -- forcing one shared model across combos that behave
    this differently would blur exactly the distinction that matters.
    """
    from apps.market_data.indicators import load_full_indicator_frame
    from apps.learning.ml_train import _should_promote
    from apps.learning.models import ModelRegistry
    from django.conf import settings

    df = load_full_indicator_frame(symbol, timeframe, limit=20000)
    if df.empty or len(df) < MIN_ROWS_REQUIRED:
        return {
            "trained": False, "reason": "insufficient_historical_data",
            "row_count": len(df) if not df.empty else 0,
        }

    features = _build_feature_frame(df)
    labels, r_multiples = _simulate_labels(
        df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), df["atr"].to_numpy(),
        atr_multiplier, max_holding_bars,
    )
    features = features.copy()
    features["__label"] = labels
    features = features.replace([np.inf, -np.inf], np.nan).dropna()

    if len(features) < MIN_ROWS_REQUIRED:
        return {
            "trained": False, "reason": "insufficient_labeled_rows_after_dropna",
            "row_count": len(features),
        }

    y = features.pop("__label").to_numpy()
    X = features[TECHNICAL_FEATURES].to_numpy()

    win_rate = float(y.mean())
    if win_rate in (0.0, 1.0):
        return {"trained": False, "reason": "single_class_only", "row_count": len(y)}

    model, metrics = _fit_and_evaluate(X, y, TECHNICAL_FEATURES)
    metrics["row_count"] = len(y)
    metrics["baseline_win_rate_full"] = round(win_rate, 4)
    metrics["atr_multiplier"] = atr_multiplier
    metrics["max_holding_bars"] = max_holding_bars
    metrics["feature_columns"] = TECHNICAL_FEATURES

    model_name = f"technical_direction_{symbol}_{timeframe}"
    champion = ModelRegistry.objects.filter(model_name=model_name, active_flag=True).first()
    promote, gate_reason = _should_promote(metrics, champion.metrics_json if champion else None)
    metrics["promotion_gate"] = {"promoted": promote, "reason": gate_reason}

    import joblib
    artifact_dir = Path(getattr(settings, "ML_ARTIFACT_DIR", settings.BASE_DIR / "ml_artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    version = datetime.now(dt_timezone.utc).strftime("%Y%m%d%H%M%S")
    artifact_path = artifact_dir / f"{model_name}_{version}.joblib"
    joblib.dump(model, artifact_path)

    if promote:
        ModelRegistry.objects.filter(model_name=model_name, active_flag=True).update(active_flag=False)
    registry_row = ModelRegistry.objects.create(
        model_name=model_name, model_version=version,
        artifact_path=str(artifact_path), metrics_json=metrics, active_flag=promote,
    )

    logger.info(
        "train_technical_direction_model(%s, %s): trained on %d labeled bars. "
        "holdout accuracy=%s auc=%s. promoted=%s (%s). registered as %s.",
        symbol, timeframe, len(y), metrics.get("accuracy"), metrics.get("roc_auc"),
        promote, gate_reason, registry_row,
    )
    return {
        "trained": True, "promoted": promote, "promotion_reason": gate_reason,
        "registry_id": registry_row.pk, "metrics": metrics,
    }


def predict_technical_direction(symbol: str, timeframe: str) -> dict | None:
    """
    Scores the LATEST live bar (via apps.market_data.indicators.
    compute_indicators, the same snapshot the rule-based signal engine
    itself sees) against the active technical_direction_{symbol}_{timeframe}
    model, if one exists. Returns None (not an exception) if no active
    model is registered yet -- same "missing model is a skip, not a
    crash" convention as every other "not enough data yet" path in this
    codebase.
    """
    from apps.learning.models import ModelRegistry
    from apps.market_data.indicators import compute_indicators, load_candle_frame, _with_indicator_columns

    model_name = f"technical_direction_{symbol}_{timeframe}"
    registry_row = ModelRegistry.objects.filter(model_name=model_name, active_flag=True).first()
    if registry_row is None:
        return None

    df = load_candle_frame(symbol, timeframe, limit=60)
    if len(df) < 30:
        return None
    df = _with_indicator_columns(df)
    features = _build_feature_frame(df).iloc[[-1]]
    if features.isna().any(axis=None):
        return None

    import joblib
    model = joblib.load(registry_row.artifact_path)
    probability_favorable = float(model.predict_proba(features[TECHNICAL_FEATURES].to_numpy())[0, 1])

    return {
        "model_version": registry_row.model_version,
        "probability_favorable": round(probability_favorable, 4),
        "trained_holdout_accuracy": registry_row.metrics_json.get("accuracy"),
        "trained_holdout_auc": registry_row.metrics_json.get("roc_auc"),
    }
