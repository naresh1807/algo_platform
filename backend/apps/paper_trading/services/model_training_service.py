"""
ModelTrainingService -- trains a challenger "paper_trading_policy"
model from apps.learning.TrainingSample rows (source_environment=
PAPER), using a chronological split with a GAP >= the prediction
horizon between the training fold and the holdout fold (spec: "Include
a gap between training and validation that is at least equal to the
prediction horizon to prevent label leakage" -- never randomly shuffled).

Reuses apps.learning.ml_train.WinProbabilityEnsemble and its private
pipeline builders (_new_logistic_pipeline/_new_gbm_pipeline/
_evaluate_on_holdout) -- the SAME artifact class/fit shape the
win_probability and scalp_win_probability model families already use,
so a saved artifact loads via the exact same
`joblib.load(...).predict_proba(...)` call ai_signal_service already
makes. Cross-module reuse of these "private" helpers matches this
codebase's own established convention (e.g. apps.options.strike_
selector reusing apps.options.metrics._latest_snapshots).
"""

from __future__ import annotations

MIN_TRAINING_SAMPLES = 30
# Matches outcome_service.compute_hypothetical_outcome's default
# horizon_bars=12 -- the number of forward bars a label's outcome can
# depend on, and therefore the minimum gap required between the last
# training row and the first holdout row.
PREDICTION_HORIZON_BARS = 12
GAP_SAMPLES = PREDICTION_HORIZON_BARS


def _is_numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _feature_columns(rows) -> list[str]:
    """Only NUMERIC feature keys become training columns -- feature_
    engineering.build_underlying_features includes at least one
    categorical string value ("regime": "trending"/"sideways"/
    "high_volatility"), and blindly float()-casting that would crash
    training. Excluding it here (rather than one-hot-encoding it) is a
    deliberate v1 simplification, documented via FEATURE_SCHEMA_VERSION
    -- a future schema version can add encoded regime columns without
    this function needing to change, only the schema version bump."""
    names: set[str] = set()
    for row in rows:
        for key, value in row.feature_vector_json.items():
            if _is_numeric(value):
                names.add(key)
    return sorted(names)


def build_dataset(model_name: str = "paper_trading_policy"):
    from apps.learning.models import TrainingSample

    return list(
        TrainingSample.objects.filter(model_name=model_name, source_environment="PAPER").order_by("timestamp")
    )


def train_challenger(model_name: str = "paper_trading_policy") -> dict:
    from apps.learning.ml_train import WinProbabilityEnsemble, _evaluate_on_holdout, _new_gbm_pipeline, _new_logistic_pipeline

    rows = build_dataset(model_name)
    if len(rows) < MIN_TRAINING_SAMPLES:
        return {"trained": False, "reason": f"only {len(rows)} training samples, need {MIN_TRAINING_SAMPLES}"}

    feature_names = _feature_columns(rows)
    X = [[float(row.feature_vector_json.get(name) or 0.0) for name in feature_names] for row in rows]
    y = [int(row.label) for row in rows]

    n = len(y)
    holdout_size = max(10, int(n * 0.25))
    split = n - holdout_size
    train_end = max(0, split - GAP_SAMPLES)
    X_train, y_train = X[:train_end], y[:train_end]
    X_holdout, y_holdout = X[split:], y[split:]

    if len(y_train) < 5 or len(set(y_train)) < 2:
        return {"trained": False, "reason": "insufficient class diversity in the training fold after the leakage gap"}

    logistic = _new_logistic_pipeline()
    logistic.fit(X_train, y_train)
    gbm = _new_gbm_pipeline()
    gbm.fit(X_train, y_train)
    ensemble = WinProbabilityEnsemble(logistic, gbm)
    metrics = _evaluate_on_holdout(ensemble, X_holdout, y_holdout)
    metrics.update({
        "eval_type": "chronological_holdout",
        "train_size": len(y_train), "gap_samples": GAP_SAMPLES, "feature_columns": feature_names,
    })

    # Deployed artifact is refit on ALL data (train + gap + holdout) --
    # the split above exists only to get an honest out-of-sample metric,
    # same convention apps.learning.ml_train._fit_and_evaluate documents.
    final_logistic = _new_logistic_pipeline()
    final_logistic.fit(X, y)
    final_gbm = _new_gbm_pipeline()
    final_gbm.fit(X, y)
    final_ensemble = WinProbabilityEnsemble(final_logistic, final_gbm)

    return {"trained": True, "ensemble": final_ensemble, "metrics": metrics, "sample_count": n, "holdout_rows": rows[split:]}
