"""
Shared feature-building for apps.learning.scalp_ml_train/scalp_ml_predict
-- the scalping-only sibling of apps.learning.ml_features, kept as its own
module (not folded into ml_features.py) since it's built from
HypotheticalTrade, not TradingSignal, and has no composite technical/
sentiment/risk/options scores to draw on (HypotheticalTrade never runs
those checks -- see that model's own docstring). RAW_INDICATOR_FEATURES is
still imported from ml_features.py rather than redefined here, since both
modules mean exactly the same thing by "ind_rsi"/"ind_atr_pct"/etc. --
apps.learning.tasks._run_comparison_cycle populates HypotheticalTrade's
ind_* columns via the exact same apps.signals.engine._indicator_signal_fields
helper TradingSignal's are built from.
"""

from __future__ import annotations

from .ml_features import RAW_INDICATOR_FEATURES

BASE_FEATURES = list(RAW_INDICATOR_FEATURES)


def feature_names_for_scalp(methods: list[str], symbols: list[str]) -> list[str]:
    """
    Full, ordered feature-column list for a given method group + watchlist:
    the raw indicator columns plus one method-indicator column per
    scalping method and one symbol-indicator column per symbol. Stored in
    ModelRegistry.metrics_json["feature_columns"] at train time (same
    convention as apps.learning.ml_features.feature_names_for), so a later
    change to SCALPING_METHOD_FUNCS/settings.WATCHLIST can never silently
    misalign an old model's expected columns with new predict-time data.
    """
    return BASE_FEATURES + [f"method_{m}" for m in methods] + [f"symbol_{s}" for s in symbols]


def vector_for_hypothetical_trade(trade, feature_names: list[str]) -> list[float]:
    """
    Builds one feature row for `trade` (a HypotheticalTrade) in exactly
    the order of `feature_names`. Same "missing feature reads as a
    neutral 0, never a crash" convention as
    apps.learning.ml_features.vector_for_signal -- a trade whose own
    ind_* columns are unset (e.g. a swing-group trade this model was never
    meant to score) or whose method/symbol has no matching column (a
    method/symbol added after the active model was last trained) just
    falls back to 0.0 for those columns rather than raising.
    """
    values: dict[str, float] = {
        "ind_rsi": float(getattr(trade, "ind_rsi", None) or 0.0) / 100.0,
        "ind_adx": float(getattr(trade, "ind_adx", None) or 0.0) / 100.0,
        "ind_bb_width": float(getattr(trade, "ind_bb_width", None) or 0.0),
        "ind_relative_volume": float(getattr(trade, "ind_relative_volume", None) or 0.0),
        "ind_atr_pct": float(getattr(trade, "ind_atr_pct", None) or 0.0),
        "ind_macd_hist_pct": float(getattr(trade, "ind_macd_hist_pct", None) or 0.0),
        "ind_ema9_slope_pct": float(getattr(trade, "ind_ema9_slope_pct", None) or 0.0),
        "ind_ema21_slope_pct": float(getattr(trade, "ind_ema21_slope_pct", None) or 0.0),
    }

    method_col = f"method_{trade.method}"
    symbol_col = f"symbol_{trade.symbol}"
    for name in feature_names:
        if name.startswith("method_"):
            values[name] = 1.0 if name == method_col else 0.0
        elif name.startswith("symbol_"):
            values[name] = 1.0 if name == symbol_col else 0.0

    return [values.get(name, 0.0) for name in feature_names]
