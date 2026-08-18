"""
Win-probability ML model for the scalping comparison group -- inference
side (see scalp_ml_train.py for training and why this is a separate model
from apps.learning.ml_predict's, scalp_ml_features.py for the shared
feature-building both use).

Advisory-only, on purpose: unlike apps.learning.ml_predict, this module has
no should_reject_for_low_confidence/ml_confidence_size_multiplier
equivalents -- apps.learning.tasks._run_comparison_cycle only ever stores
this prediction on the HypotheticalTrade it scores, never uses it to skip
opening a trade. Gating entries by this model's own prediction would bias
the very comparison (apps.learning.tasks.evaluate_scalping_strategy_methods)
this data is meant to feed -- see scalp_ml_train's module docstring.
"""

from __future__ import annotations

import logging
import threading

from .scalp_ml_train import MODEL_NAME

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_cached_model = None
_cached_registry_id = None
_cached_feature_columns: list[str] | None = None


def _load_active_model():
    """
    Lazily loads (and caches in-process) the currently-active
    scalp_win_probability ModelRegistry row's artifact -- own cache,
    keyed on its own registry row id, entirely separate from
    apps.learning.ml_predict's cache for the real model. Re-checks the DB
    on every call (one cheap indexed query), same as that module.
    """
    global _cached_model, _cached_registry_id, _cached_feature_columns
    from apps.learning.models import ModelRegistry

    row = ModelRegistry.objects.filter(model_name=MODEL_NAME, active_flag=True).first()
    if row is None:
        return None, None

    with _cache_lock:
        if row.pk != _cached_registry_id:
            import joblib

            try:
                _cached_model = joblib.load(row.artifact_path)
                _cached_feature_columns = row.metrics_json.get("feature_columns")
                _cached_registry_id = row.pk
            except Exception:
                # Broad on purpose -- same reasoning as ml_predict.
                # _load_active_model's identical guard: a corrupted
                # artifact, version mismatch, or permissions error must
                # all fall back to "no active model," not an exception
                # escaping into the comparison-cycle task that calls this.
                logger.warning(
                    "scalp_ml_predict: active ModelRegistry row %s points at an unusable "
                    "artifact (%s) -- treating as no active model.",
                    row.pk, row.artifact_path, exc_info=True,
                )
                return None, None
        return _cached_model, _cached_feature_columns


def predict_scalp_win_probability(trade) -> float | None:
    """
    trade: an apps.learning.models.HypotheticalTrade instance (saved or
    not) from the scalping method group, with its ind_* columns already
    set. Returns a probability in [0, 1], or None if no active model is
    registered yet (cold start -- expected until
    scalp_ml_train.MIN_TRAINING_SAMPLES closed scalps exist) or scoring
    fails. Never raises -- a scoring failure here must not break the
    comparison cycle, which is the actual data-collection-critical path.
    """
    from .scalp_ml_features import vector_for_hypothetical_trade

    model, feature_columns = _load_active_model()
    if model is None or not feature_columns:
        return None

    try:
        vector = vector_for_hypothetical_trade(trade, feature_columns)
        probability = float(model.predict_proba([vector])[0][1])
        return round(probability, 4)
    except Exception:
        logger.exception(
            "scalp_ml_predict: failed to score trade (method=%s symbol=%s) -- leaving "
            "ml_win_probability unset.", getattr(trade, "method", "?"), getattr(trade, "symbol", "?"),
        )
        return None
