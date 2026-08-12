"""
Win-probability ML model -- training side (see ml_predict.py for
inference, ml_features.py for the shared feature-building both use).

This is additive to the rule-based signals engine (apps/signals/engine.py),
not a replacement: generate_signal() still makes every BUY/NO_TRADE
decision the same way it always did, and apps/risk/engine.py's hard
limits (kill switch, drawdown pause, daily loss limit) are never
touched by anything in this module. This model predicts, after the
fact, "how often did setups that looked like this one actually win" --
it is a probability estimate, not a promise. No model, however
well-trained, eliminates losing trades; the honest goal here is a
*calibrated* probability that lets position sizing and human review
lean into higher-confidence setups and away from low-confidence ones --
not a claim that losses stop happening.

Model choice: logistic regression over a small, fixed, interpretable
feature set (the four existing composite scores + one-hot regime +
one-hot symbol). Deliberately NOT a bigger model (gradient boosting /
neural net) while trade counts are still small (tens to low hundreds
after a month or two of paper trading) -- a high-capacity model on a
tiny sample mostly learns noise, and logistic regression's coefficients
are directly readable ("which factor is actually associated with
wins"), matching the manual's "AI must explain every signal" principle
even at the ML layer. Swap in GradientBoostingClassifier / XGBoost here
once there's a few hundred+ closed trades to train on -- the
surrounding pipeline (feature building, versioning via ModelRegistry,
chronological validation, champion/challenger gate) does not need to
change to do that.

Production-grade pieces in this module, beyond the first pass:
  1. CHRONOLOGICAL validation split, not random -- a random split on
     time-ordered trades lets the model "see the future" during
     validation, which inflates the reported accuracy versus what
     you'd actually get trading forward in time. The holdout here is
     always the most-recent N% of closed trades, sorted by
     closed_at -- the same walk-forward principle the manual's section
     19 backtesting standards call for, applied to this model's own
     evaluation.
  2. CHAMPION/CHALLENGER promotion gate -- a newly-trained model
     ("challenger") only replaces the current active model
     ("champion") if it is not meaningfully worse on its own holdout.
     Mirrors the same "never silently regress" principle already used
     for StrategyVersion/DailyReviewNote: retraining drafts a
     challenger, and this function is the (automatic, but
     conservative) approval gate -- a bad batch of recent trades can't
     silently make the live model worse.
  3. Brier score (calibration quality, lower is better) reported
     alongside accuracy/AUC -- accuracy alone can look fine while the
     probabilities themselves are miscalibrated, which would make
     confidence-based position sizing misleading.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Below this many closed trades, don't even attempt a fit -- a model
# "trained" on a handful of trades would just memorize noise and report
# a false sense of confidence. Returning "not trained yet" is the
# honest answer, same spirit as apps.analytics.services leaving
# max_drawdown as None rather than fabricating a number.
MIN_TRAINING_SAMPLES = 30

# Below this many closed trades, train on 100% of the data and report
# train-set-only metrics (clearly labelled as optimistic) rather than
# burning a chunk of an already-small dataset on a held-out split.
MIN_SAMPLES_FOR_HOLDOUT = 60

# Fraction of closed trades (the most RECENT slice, chronologically)
# held out for evaluation once there's enough data -- see the module
# docstring's point 1 for why this must be time-ordered, not random.
HOLDOUT_FRACTION = 0.25

# A challenger model is only promoted over the current champion if its
# holdout accuracy isn't worse than the champion's own recorded holdout
# accuracy by more than this margin. Sized loosely on purpose -- with
# holdout sets in the tens of trades, small accuracy swings are mostly
# noise, not a real regression; this only blocks a clearly worse model
# (e.g. a bad recent batch of trades pulling the model off course), not
# every minor fluctuation.
MAX_ACCEPTABLE_ACCURACY_REGRESSION = 0.05


def _training_rows():
    """
    Yields (TradingSignal, won: bool) for every closed OpenPosition,
    oldest first. One row per *position*, not per signal, since a
    signal only has a known outcome once a position opened from it has
    actually closed -- matches check_for_drift's own definition of a
    "trade" in apps/learning/tasks.py.
    """
    from apps.execution.models import OpenPosition

    closed = (
        OpenPosition.objects
        .filter(closed_at__isnull=False, signal__isnull=False)
        .select_related("signal")
        .order_by("closed_at")
    )
    for position in closed:
        yield position.signal, bool(position.unrealized_pnl > 0)


def _fit_and_evaluate(X: list, y: list) -> tuple:
    """
    Returns (fitted_pipeline_on_all_data, metrics_dict). Chronological
    holdout when there's enough data (module docstring point 1);
    train-only metrics (clearly labelled optimistic) otherwise.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    def _new_pipeline():
        return Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ])

    n = len(y)
    if n >= MIN_SAMPLES_FOR_HOLDOUT:
        split = int(n * (1 - HOLDOUT_FRACTION))
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        eval_pipeline = _new_pipeline()
        eval_pipeline.fit(X_train, y_train)
        preds = eval_pipeline.predict(X_test)
        probs = eval_pipeline.predict_proba(X_test)[:, 1]

        metrics = {
            "eval_type": "chronological_holdout",
            "holdout_size": len(y_test),
            "accuracy": round(float(accuracy_score(y_test, preds)), 4),
            "brier_score": round(float(brier_score_loss(y_test, probs)), 4),
        }
        if len(set(y_test)) == 2:  # AUC undefined with one class in the holdout
            metrics["roc_auc"] = round(float(roc_auc_score(y_test, probs)), 4)
        else:
            metrics["note_holdout"] = (
                "Holdout slice contains only one outcome class -- ROC-AUC not "
                "computable; accuracy/Brier score still reported but treat with "
                "extra caution until a more balanced holdout is available."
            )
    else:
        eval_pipeline = _new_pipeline()
        eval_pipeline.fit(X, y)
        preds = eval_pipeline.predict(X)
        metrics = {
            "eval_type": "train_only_no_holdout",
            "note": (
                f"Only {n} closed trades -- below the {MIN_SAMPLES_FOR_HOLDOUT} "
                "needed for a chronological holdout, so this accuracy is measured "
                "on the training data itself and is optimistic. Treat it as a "
                "sanity check, not a real performance estimate, until more trades "
                "close."
            ),
            "accuracy": round(float(accuracy_score(y, preds)), 4),
        }

    # Final deployed artifact is refit on ALL data -- the holdout split
    # above exists only to get an honest, unbiased metric; throwing away
    # the most recent slice of a still-small dataset in the live model
    # would waste exactly the data most relevant to current conditions.
    final_pipeline = _new_pipeline()
    final_pipeline.fit(X, y)
    return final_pipeline, metrics


def _should_promote(challenger_metrics: dict, champion_metrics: dict | None) -> tuple[bool, str]:
    """
    Champion/challenger gate (module docstring point 2). No champion
    yet -> always promote. Otherwise, only promote if the challenger's
    holdout accuracy isn't worse than the champion's own recorded
    holdout accuracy by more than MAX_ACCEPTABLE_ACCURACY_REGRESSION.
    If either side lacks a real holdout accuracy (train-only metrics),
    promote anyway -- there's nothing comparable to gate on yet, and
    refusing to ever update the very first models would defeat the
    point of retraining as more data arrives.
    """
    if champion_metrics is None:
        return True, "no_existing_champion"

    challenger_acc = challenger_metrics.get("accuracy")
    champion_acc = champion_metrics.get("accuracy")
    if (
        challenger_metrics.get("eval_type") != "chronological_holdout"
        or champion_metrics.get("eval_type") != "chronological_holdout"
        or challenger_acc is None or champion_acc is None
    ):
        return True, "insufficient_comparable_holdout_metrics"

    if challenger_acc >= champion_acc - MAX_ACCEPTABLE_ACCURACY_REGRESSION:
        return True, f"challenger_accuracy_{challenger_acc:.3f}_vs_champion_{champion_acc:.3f}"

    return False, f"challenger_accuracy_{challenger_acc:.3f}_regresses_below_champion_{champion_acc:.3f}"


def train_win_probability_model() -> dict:
    """
    Trains a challenger model from every closed trade so far. Promotes
    it to active (deactivating the previous "win_probability" version)
    ONLY if it passes the champion/challenger gate above -- otherwise
    it is still saved to ModelRegistry (active_flag=False) so its
    metrics are visible/auditable, but the currently-active model keeps
    serving predictions. Never raises on "not enough data yet" or "only
    wins/only losses so far" -- both are expected, common states in the
    first weeks of paper trading, not errors.
    """
    from django.conf import settings

    from apps.learning.ml_features import feature_names_for, vector_for_signal
    from apps.learning.models import ModelRegistry

    rows = list(_training_rows())
    if len(rows) < MIN_TRAINING_SAMPLES:
        logger.info(
            "train_win_probability_model: only %d closed trades so far (need >= %d) "
            "-- skipping training.", len(rows), MIN_TRAINING_SAMPLES,
        )
        return {"trained": False, "reason": "insufficient_data", "sample_count": len(rows)}

    symbols = list(settings.WATCHLIST)
    feature_names = feature_names_for(symbols)
    X = [vector_for_signal(sig, feature_names) for sig, _won in rows]  # already time-ordered
    y = [int(won) for _sig, won in rows]

    win_rate = sum(y) / len(y)
    if win_rate in (0.0, 1.0):
        logger.warning(
            "train_win_probability_model: every closed trade so far is a %s -- "
            "cannot train a classifier on a single class yet.",
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

    champion = ModelRegistry.objects.filter(model_name="win_probability", active_flag=True).first()
    promote, gate_reason = _should_promote(metrics, champion.metrics_json if champion else None)
    metrics["promotion_gate"] = {"promoted": promote, "reason": gate_reason}

    import joblib

    artifact_dir = Path(getattr(settings, "ML_ARTIFACT_DIR", settings.BASE_DIR / "ml_artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    version = datetime.now(dt_timezone.utc).strftime("%Y%m%d%H%M%S")
    artifact_path = artifact_dir / f"win_probability_{version}.joblib"
    joblib.dump(pipeline, artifact_path)

    if promote:
        ModelRegistry.objects.filter(model_name="win_probability", active_flag=True).update(active_flag=False)
    registry_row = ModelRegistry.objects.create(
        model_name="win_probability",
        model_version=version,
        artifact_path=str(artifact_path),
        metrics_json=metrics,
        active_flag=promote,
    )

    logger.info(
        "train_win_probability_model: trained on %d closed trades (%s). "
        "promoted=%s (%s). registered as %s.",
        len(rows), metrics.get("eval_type"), promote, gate_reason, registry_row,
    )
    return {
        "trained": True, "promoted": promote, "promotion_reason": gate_reason,
        "registry_id": registry_row.pk, "metrics": metrics,
    }
