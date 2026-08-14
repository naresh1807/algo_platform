"""
Win-probability ML model -- inference side (see ml_train.py for
training and the reasoning behind the model choice; ml_features.py for
the feature-building shared by both).
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_cached_model = None
_cached_registry_id = None
_cached_feature_columns: list[str] | None = None


def _load_active_model():
    """
    Lazily loads (and caches in-process) the currently-active
    win_probability ModelRegistry row's artifact. Re-checks the DB on
    every call (one cheap indexed query) so a freshly-trained/promoted
    model is picked up without needing a worker restart -- only the
    comparatively expensive joblib.load is actually cached, keyed on
    registry row id.
    """
    global _cached_model, _cached_registry_id, _cached_feature_columns
    from apps.learning.models import ModelRegistry

    row = ModelRegistry.objects.filter(model_name="win_probability", active_flag=True).first()
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
                # Broad on purpose, not just FileNotFoundError -- a
                # corrupted artifact, a pickle/sklearn-version mismatch,
                # or a permissions error must all fall back to "no active
                # model" the same way a missing file does, since this
                # function is called directly (unguarded) from
                # predict_win_probability, whose own docstring promises
                # it never raises -- letting anything but FileNotFoundError
                # escape here broke that promise for every other joblib
                # load failure.
                logger.warning(
                    "ml_predict: active ModelRegistry row %s points at an unusable "
                    "artifact (%s) -- treating as no active model.",
                    row.pk, row.artifact_path, exc_info=True,
                )
                return None, None
        return _cached_model, _cached_feature_columns


def predict_win_probability(signal) -> float | None:
    """
    signal: an apps.signals.models.TradingSignal instance with its
    score fields and regime already set (saved or not). Returns a
    probability in [0, 1], or None if no active model is registered yet
    (cold start -- expected during the first weeks of paper trading, see
    ml_train.MIN_TRAINING_SAMPLES) or the model artifact can't be
    loaded. Never raises -- a scoring failure here must not break signal
    generation, which is the actual trading-critical path.
    """
    from apps.learning.ml_features import vector_for_signal

    model, feature_columns = _load_active_model()
    if model is None or not feature_columns:
        return None

    try:
        vector = vector_for_signal(signal, feature_columns)
        probability = float(model.predict_proba([vector])[0][1])
        return round(probability, 4)
    except Exception:
        logger.exception(
            "ml_predict: failed to score signal (symbol=%s) -- leaving "
            "ml_win_probability unset.", getattr(signal, "symbol", "?"),
        )
        return None


# Narrow, hard-coded bounds -- this is a position-SIZE nudge on an
# already risk-approved trade, never a second trading decision. See
# ml_confidence_size_multiplier's own docstring for the full reasoning.
_MIN_CONFIDENCE_MULTIPLIER = 0.7
_MAX_CONFIDENCE_MULTIPLIER = 1.15

# Hard veto threshold: a setup that already passed technical + sentiment
# + options + risk still gets turned away here if the win-probability
# model has actually been trained (probability is not None) AND scores
# it below this bar. This is the "should the system give this kind of
# trade again" gate the daily learning loop feeds -- distinct from
# ml_confidence_size_multiplier above, which only ever nudges size on a
# trade that's already going through. Set at 0.5 -- a strict coin-flip
# bar -- per explicit request; note this is stricter than a
# conservative 0.35 floor would be, so expect more setups to be
# rejected as NO_TRADE, especially early on while the model has few
# closed trades to learn from and its probability estimates carry more
# uncertainty (see should_reject_for_low_confidence's docstring).
MIN_WIN_PROBABILITY_TO_TRADE = 0.50


def should_reject_for_low_confidence(probability: float | None) -> bool:
    """
    Returns True if this signal should be turned into a NO_TRADE purely
    on the ML model's own past-performance read, i.e. "setups that
    looked like this one mostly lost, historically -- don't repeat
    that." This is what makes the learning loop's daily retrain
    actually change *whether* a trade is taken, not just its size.

    probability=None (no trained model yet, or scoring failed) never
    rejects -- same cold-start reasoning as
    ml_confidence_size_multiplier: with no model, the system behaves
    exactly as it did before ML existed, it does not fail closed.
    MIN_WIN_PROBABILITY_TO_TRADE is a strict 50% bar: with a small,
    still-growing sample of closed trades, the model's probability
    estimate carries real uncertainty of its own, so expect this to
    reject more setups (including some that would have won) while the
    model is young -- that tradeoff was chosen deliberately over a
    looser floor. No model can promise only "sure things"; this still
    isn't a guarantee, just a stricter filter.
    """
    if probability is None:
        return False
    return probability < MIN_WIN_PROBABILITY_TO_TRADE


def ml_confidence_size_multiplier(probability: float | None) -> float:
    """
    Bounded, purely-scaling multiplier for position sizing based on the
    ML win-probability -- meant to be applied only AFTER
    apps.risk.engine has already approved a trade and computed its own
    base position size (see apps/signals/engine.py's usage). Stacks
    multiplicatively with the existing regime-based size multiplier,
    same pattern the risk engine already uses for
    drawdown-based/expiry-day size reductions -- this never replaces
    those, and apps.risk.engine's hard limits (settings.RISK_HARD_LIMITS,
    max-position/exposure checks) still apply on top of whatever size
    comes out of this.

    probability=None (no model trained yet, or this signal couldn't be
    scored) returns 1.0 -- no adjustment -- so the system behaves
    exactly as it did before any ML model existed until there's a real
    model to lean on. probability=0.5 (a coin-flip, i.e. the model sees
    no edge either way) also returns 1.0 deliberately, for the same
    reason: this only nudges size when the model actually disagrees
    with "no information", not as a default behavior.
    """
    if probability is None:
        return 1.0

    raw = 1.0 + (probability - 0.5) * 0.6
    return round(max(_MIN_CONFIDENCE_MULTIPLIER, min(_MAX_CONFIDENCE_MULTIPLIER, raw)), 4)
