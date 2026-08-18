"""
Win-probability ML model for the SCALPING comparison group only
(apps.learning.strategy_methods.SCALPING_METHOD_FUNCS) -- see
ml_train.py's own module docstring for the shared reasoning (model
choice, chronological holdout, champion/challenger gate, Brier score);
this module reuses that one's _fit_and_evaluate/_should_promote helpers
directly rather than duplicating them, since neither is TradingSignal-
specific (both operate on plain X/y and metrics dicts).

DELIBERATELY SEPARATE from ml_train.train_win_probability_model, not an
extra data source folded into it: apps.learning.models.HypotheticalTrade's
own docstring already documents, at length, why hypothetical/comparison
trades must stay fully isolated from the real strategy's ML training data
-- a different kind of trade (isolated paper comparison, no risk/sentiment/
options gate, always qty=1) silently leaking into the model that scores
and VETOES real trades was flagged as a real risk there, not a hypothetical
one. This module's model is registered under its own ModelRegistry
model_name ("scalp_win_probability", vs. "win_probability") and is purely
advisory when used (apps.learning.scalp_ml_predict.
predict_scalp_win_probability, called from apps.learning.tasks.
_run_comparison_cycle) -- it never gates which scalp ideas open, so the
daily method-comparison ranking (apps.learning.tasks.
evaluate_scalping_strategy_methods) keeps measuring each method's own
unfiltered performance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Same values as ml_train.py's identically-named constants, kept as their
# own copies (not imported) since the two models' sample sizes grow on
# unrelated schedules (real trades vs. 1-minute scalp comparisons) and
# tuning one's thresholds should never accidentally move the other's.
MIN_TRAINING_SAMPLES = 30
MIN_SAMPLES_FOR_HOLDOUT = 60
HOLDOUT_FRACTION = 0.25
MAX_ACCEPTABLE_ACCURACY_REGRESSION = 0.05

MODEL_NAME = "scalp_win_probability"


def _training_rows():
    """
    Yields (HypotheticalTrade, won: bool) for every closed scalping-group
    trade, oldest first -- method list read from strategy_methods.
    SCALPING_METHOD_FUNCS (never hardcoded) so this can't silently drift
    from the real method group if one is ever added/removed. Swing-group
    (METHOD_FUNCS) trades are excluded outright, not just unscored -- see
    module docstring on why this model must never see them.
    """
    from .models import HypotheticalTrade
    from .strategy_methods import SCALPING_METHOD_FUNCS

    closed = (
        HypotheticalTrade.objects
        .filter(method__in=SCALPING_METHOD_FUNCS, closed_at__isnull=False)
        .order_by("closed_at")
    )
    for trade in closed:
        yield trade, bool(trade.pnl is not None and trade.pnl > 0)


def train_scalp_win_probability_model() -> dict:
    """
    Trains a challenger model from every closed scalping-group trade so
    far, gated through the same champion/challenger promotion logic
    ml_train.train_win_probability_model uses (imported, not
    reimplemented) -- promotes to active (deactivating the previous
    "scalp_win_probability" version) only if it isn't meaningfully worse
    on its own holdout. Never raises on "not enough data yet" or "only
    wins/only losses so far" -- both expected, common states early on.
    """
    from django.conf import settings

    from apps.learning.ml_train import _fit_and_evaluate, _should_promote
    from apps.learning.models import ModelRegistry

    from .scalp_ml_features import feature_names_for_scalp, vector_for_hypothetical_trade
    from .strategy_methods import SCALPING_METHOD_FUNCS

    rows = list(_training_rows())
    if len(rows) < MIN_TRAINING_SAMPLES:
        logger.info(
            "train_scalp_win_probability_model: only %d closed scalping trades so far "
            "(need >= %d) -- skipping training.", len(rows), MIN_TRAINING_SAMPLES,
        )
        return {"trained": False, "reason": "insufficient_data", "sample_count": len(rows)}

    methods = list(SCALPING_METHOD_FUNCS)
    symbols = list(settings.WATCHLIST)
    feature_names = feature_names_for_scalp(methods, symbols)
    X = [vector_for_hypothetical_trade(trade, feature_names) for trade, _won in rows]
    y = [int(won) for _trade, won in rows]

    win_rate = sum(y) / len(y)
    if win_rate in (0.0, 1.0):
        logger.warning(
            "train_scalp_win_probability_model: every closed scalping trade so far is a "
            "%s -- cannot train a classifier on a single class yet.",
            "win" if win_rate == 1.0 else "loss",
        )
        return {"trained": False, "reason": "single_class_only", "sample_count": len(rows)}

    pipeline, metrics = _fit_and_evaluate(X, y)
    metrics["sample_count"] = len(rows)
    metrics["baseline_win_rate"] = round(win_rate, 4)
    metrics["feature_columns"] = feature_names
    metrics["feature_coefficients"] = dict(
        zip(feature_names, pipeline.named_steps["clf"].coef_[0].round(4).tolist())
    )

    champion = ModelRegistry.objects.filter(model_name=MODEL_NAME, active_flag=True).first()
    promote, gate_reason = _should_promote(metrics, champion.metrics_json if champion else None)
    metrics["promotion_gate"] = {"promoted": promote, "reason": gate_reason}

    import joblib

    artifact_dir = Path(getattr(settings, "ML_ARTIFACT_DIR", settings.BASE_DIR / "ml_artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    version = datetime.now(dt_timezone.utc).strftime("%Y%m%d%H%M%S")
    artifact_path = artifact_dir / f"{MODEL_NAME}_{version}.joblib"
    joblib.dump(pipeline, artifact_path)

    if promote:
        ModelRegistry.objects.filter(model_name=MODEL_NAME, active_flag=True).update(active_flag=False)
    registry_row = ModelRegistry.objects.create(
        model_name=MODEL_NAME,
        model_version=version,
        artifact_path=str(artifact_path),
        metrics_json=metrics,
        active_flag=promote,
    )

    logger.info(
        "train_scalp_win_probability_model: trained on %d closed scalping trades (%s). "
        "promoted=%s (%s). registered as %s.",
        len(rows), metrics.get("eval_type"), promote, gate_reason, registry_row,
    )
    return {
        "trained": True, "promoted": promote, "promotion_reason": gate_reason,
        "registry_id": registry_row.pk, "metrics": metrics,
    }
