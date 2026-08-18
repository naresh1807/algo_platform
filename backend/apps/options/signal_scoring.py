"""
Signal Scoring Engine: combines apps.options.confirmation's per-factor
breakdown into ONE transparent weighted score, using RUNTIME-
CONFIGURABLE weights (apps.options.models.ScoringWeights) -- per this
platform's own instruction that fixed weights "MUST NOT be treated as
universal."

Categorical factor signals are mapped to a 0-1 numeric score:
bullish/favorable=1.0, neutral=0.5, bearish/unfavorable=0.0,
unavailable=EXCLUDED (its weight is redistributed proportionally among
the factors that DO have data, rather than silently scored as 0 --
"no data" and "confirmed bad" are not the same thing, and treating
them the same would punish a signal for a data gap it didn't cause).
"""

from __future__ import annotations

SIGNAL_TO_SCORE = {
    "bullish": 1.0, "favorable": 1.0,
    "neutral": 0.5,
    "bearish": 0.0, "unfavorable": 0.0,
    "unavailable": None,
}


def calculate_signal_score(confirmation_result: dict, weights: dict | None = None) -> dict:
    """
    confirmation_result: apps.options.confirmation.
    evaluate_multi_signal_confirmation's return value.
    weights: optional {factor_name: weight} override; defaults to
    apps.options.models.get_scoring_weights().as_dict() when omitted.

    Returns {"total_score": 0..1 | None, "per_factor_score": {name:
    0..1|None}, "weights_used": {...}, "factors_excluded": [names]}.
    total_score is None (not a misleading 0) only when EVERY factor is
    unavailable -- a score built from zero real inputs isn't a score.
    """
    if weights is None:
        from .models import get_scoring_weights

        weights = get_scoring_weights().as_dict()

    all_factors = {
        **confirmation_result.get("directional_factors", {}),
        **confirmation_result.get("setup_quality_factors", {}),
    }

    per_factor_score: dict[str, float | None] = {}
    excluded: list[str] = []
    for name, factor in all_factors.items():
        score = SIGNAL_TO_SCORE.get(factor.get("signal"))
        per_factor_score[name] = score
        if score is None:
            excluded.append(name)

    available_weight_total = sum(
        weights.get(name, 0) for name, score in per_factor_score.items() if score is not None
    )
    if available_weight_total == 0:
        return {
            "total_score": None, "per_factor_score": per_factor_score,
            "weights_used": weights, "factors_excluded": excluded,
        }

    weighted_sum = sum(
        score * weights.get(name, 0) for name, score in per_factor_score.items() if score is not None
    )
    total_score = weighted_sum / available_weight_total  # renormalized over only the weight actually available

    return {
        "total_score": round(total_score, 4), "per_factor_score": per_factor_score,
        "weights_used": weights, "factors_excluded": excluded,
    }
