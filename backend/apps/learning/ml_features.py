"""
Shared feature-building for the win-probability ML model, used by both
ml_train.py (building the training matrix from historical closed
trades) and ml_predict.py (scoring a brand-new signal) -- kept in one
place specifically so training and inference can never silently drift
apart on what a given feature name means or what order columns come in.
"""

from __future__ import annotations

from common.constants import MarketRegime

_REGIME_FEATURES = [
    ("regime_trending", MarketRegime.TRENDING),
    ("regime_sideways", MarketRegime.SIDEWAYS),
    ("regime_high_volatility", MarketRegime.HIGH_VOLATILITY),
]

# The four existing rule-engine scores plus one-hot regime, plus a
# handful of raw indicator values captured on TradingSignal at
# signal-creation time (apps.signals.engine._indicator_signal_fields) --
# added specifically to let the win-probability model see more than the
# composite scores' summary of "how strong was this setup," since two
# setups can share the same technical_score for different underlying
# reasons (e.g. RSI just above threshold vs. deep momentum, tight vs.
# wide Bollinger bands). Kept as a fixed, explicit list (not "every
# ind_* field on the model") so a future new ind_* column doesn't
# silently change an already-trained model's expected input shape --
# extending this list is a deliberate retrain, same as extending
# BASE_FEATURES always has been.
#
# rsi/adx are rescaled from their native 0-100 range to 0-1 to sit on
# roughly the same scale as the composite scores above -- StandardScaler
# in ml_train's pipeline makes this a matter of interpretability
# (feature_coefficients stay easy to eyeball), not correctness, but
# consistent scale conventions across BASE_FEATURES make that column
# easier to reason about when someone actually reads the coefficients.
RAW_INDICATOR_FEATURES = [
    "ind_rsi", "ind_adx", "ind_bb_width", "ind_relative_volume",
    "ind_atr_pct", "ind_macd_hist_pct", "ind_ema9_slope_pct", "ind_ema21_slope_pct",
]

BASE_FEATURES = [
    "technical_score", "sentiment_score", "risk_score", "options_score",
] + [name for name, _ in _REGIME_FEATURES] + RAW_INDICATOR_FEATURES


def feature_names_for(symbols: list[str]) -> list[str]:
    """
    Full, ordered feature-column list for a given watchlist: the fixed
    base features plus one symbol-indicator column per symbol. This
    exact list (not settings.WATCHLIST re-read later) is what gets
    stored in ModelRegistry.metrics_json["feature_columns"] at train
    time, so a later change to settings.WATCHLIST can never silently
    misalign an old model's expected columns with new predict-time data.
    """
    return BASE_FEATURES + [f"symbol_{s}" for s in symbols]


def vector_for_signal(signal, feature_names: list[str]) -> list[float]:
    """
    Builds one feature row for `signal` in exactly the order of
    `feature_names`. Any symbol_<X> column not matching this signal's
    own symbol is 0.0; if the signal's symbol has no matching column at
    all (e.g. scoring a symbol added to the watchlist after the active
    model was last trained), every symbol column is 0.0 for it rather
    than raising -- the model just falls back to the base score/regime
    features for that row.

    ind_* fields default to 0.0 when unset (older rows saved before
    these columns existed, or the no-historical-data NO_TRADE branch,
    which never reaches a real indicator snapshot) -- same "missing
    feature reads as a neutral 0, never a crash" convention already
    used for the four composite scores above.
    """
    values: dict[str, float] = {
        "technical_score": float(signal.technical_score or 0.0),
        "sentiment_score": float(signal.sentiment_score or 0.0),
        "risk_score": float(signal.risk_score or 0.0),
        "options_score": float(signal.options_score or 0.0),
        "ind_rsi": float(getattr(signal, "ind_rsi", None) or 0.0) / 100.0,
        "ind_adx": float(getattr(signal, "ind_adx", None) or 0.0) / 100.0,
        "ind_bb_width": float(getattr(signal, "ind_bb_width", None) or 0.0),
        "ind_relative_volume": float(getattr(signal, "ind_relative_volume", None) or 0.0),
        "ind_atr_pct": float(getattr(signal, "ind_atr_pct", None) or 0.0),
        "ind_macd_hist_pct": float(getattr(signal, "ind_macd_hist_pct", None) or 0.0),
        "ind_ema9_slope_pct": float(getattr(signal, "ind_ema9_slope_pct", None) or 0.0),
        "ind_ema21_slope_pct": float(getattr(signal, "ind_ema21_slope_pct", None) or 0.0),
    }
    for name, regime_value in _REGIME_FEATURES:
        values[name] = 1.0 if signal.regime == regime_value else 0.0

    symbol_col = f"symbol_{signal.symbol}"
    for name in feature_names:
        if name.startswith("symbol_"):
            values[name] = 1.0 if name == symbol_col else 0.0

    return [values.get(name, 0.0) for name in feature_names]
