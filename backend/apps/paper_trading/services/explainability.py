"""
Model-native feature importance/coefficients -- not SHAP (not a
dependency of this codebase; the spec explicitly allows "model-native
importance... depending on the selected model"). Works for both halves
of ai_signal_service's ensemble (apps.learning.ml_train.
WinProbabilityEnsemble: a LogisticRegression pipeline + a
GradientBoostingClassifier) and the pure-heuristic fallback path.

Never claims causality (spec's own repeated caveat) -- output is
labelled "positive_factors"/"negative_factors" (directionally, what
pushed the score up/down), not "causes."
"""

from __future__ import annotations


def explain_heuristic(rule_scores: dict[str, float]) -> dict:
    """rule_scores: {feature_name: signed_contribution} from
    ai_signal_service's heuristic scorer. Splits into positive/negative
    factors sorted by magnitude, top 5 each."""
    positive = sorted(((k, v) for k, v in rule_scores.items() if v > 0), key=lambda kv: -kv[1])[:5]
    negative = sorted(((k, v) for k, v in rule_scores.items() if v < 0), key=lambda kv: kv[1])[:5]
    return {
        "method": "heuristic_rule_trace",
        "positive_factors": [{"feature": k, "contribution": round(v, 4)} for k, v in positive],
        "negative_factors": [{"feature": k, "contribution": round(v, 4)} for k, v in negative],
    }


def explain_model(ensemble, feature_vector: list[float], feature_names: list[str]) -> dict:
    """Best-effort: LogisticRegression sub-model coefficients x the
    actual feature value (a first-order local contribution, the same
    convention as a naive linear "SHAP-like" attribution) when the
    ensemble exposes one; falls back to the GBM's global
    feature_importances_ (not per-decision, but still model-native and
    honestly labelled as such) otherwise."""
    try:
        logistic = getattr(ensemble, "logistic_pipeline", None)
        clf = logistic.named_steps.get("clf") if logistic is not None else None
        scaler = logistic.named_steps.get("scaler") if logistic is not None else None
        if clf is not None and hasattr(clf, "coef_"):
            scaled = scaler.transform([feature_vector])[0] if scaler is not None else feature_vector
            contributions = {
                name: float(coef * value)
                for name, coef, value in zip(feature_names, clf.coef_[0], scaled)
            }
            return explain_heuristic(contributions) | {"method": "logistic_coefficient_x_value"}
    except Exception:
        pass

    try:
        gbm = getattr(ensemble, "gbm_pipeline", None)
        if gbm is not None and hasattr(gbm, "feature_importances_"):
            ranked = sorted(zip(feature_names, gbm.feature_importances_), key=lambda kv: -kv[1])[:5]
            return {
                "method": "gbm_global_feature_importance",
                "positive_factors": [{"feature": k, "contribution": round(float(v), 4)} for k, v in ranked],
                "negative_factors": [],
            }
    except Exception:
        pass

    return {"method": "unavailable", "positive_factors": [], "negative_factors": []}
