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

Model choice: a small, transparent ENSEMBLE of two models over the
same small, fixed, interpretable feature set (the four existing
composite scores + one-hot regime + one-hot symbol) -- logistic
regression (directly-readable coefficients, matches the manual's "AI
must explain every signal" principle even at the ML layer) AVERAGED
with a GradientBoostingClassifier (sklearn's built-in implementation,
no new dependency -- can pick up non-linear feature interactions the
linear model can't see). "Compare models using out-of-sample
performance... combine using a transparent ensemble... do not let one
model blindly override the others" applied literally: WinProbability
Ensemble below is a plain, equal-weight average of the two models' own
predict_proba outputs, nothing stacked/meta-learned that could itself
overfit on a still-small dataset. Both sub-models AND the ensemble are
each evaluated independently on the same chronological holdout (see
_fit_and_evaluate) so it's visible whether the ensemble is actually
earning its complexity over either model alone.

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


class WinProbabilityEnsemble:
    """
    Averages the calibrated probability from two independently-fitted
    sub-models (a LogisticRegression pipeline and a GradientBoosting
    pipeline) -- see module docstring for why. Exposes predict_proba/
    predict with the EXACT shape a plain sklearn estimator returns, so
    apps.learning.ml_predict's existing
    `joblib.load(...).predict_proba([vector])` call site needs ZERO
    changes -- this is a drop-in replacement artifact, not a new
    inference code path. Must stay defined at this stable module path
    (apps.learning.ml_train.WinProbabilityEnsemble) for joblib/pickle to
    resolve it correctly when a previously-saved artifact is reloaded.
    """

    def __init__(self, logistic_pipeline, gbm_pipeline):
        self.logistic_pipeline = logistic_pipeline
        self.gbm_pipeline = gbm_pipeline

    def predict_proba(self, X):
        logistic_probs = self.logistic_pipeline.predict_proba(X)
        gbm_probs = self.gbm_pipeline.predict_proba(X)
        return (logistic_probs + gbm_probs) / 2.0

    def predict(self, X):
        probs = self.predict_proba(X)
        return (probs[:, 1] >= 0.5).astype(int)


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


def _new_logistic_pipeline():
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
    ])


def _new_gbm_pipeline():
    # No StandardScaler -- tree-based models are scale-invariant, so
    # scaling would only add compute with no effect on the fit.
    from sklearn.ensemble import GradientBoostingClassifier

    return GradientBoostingClassifier(
        # Deliberately shallow/slow-learning defaults for a still-small
        # dataset (tens to low hundreds of trades): a deep, fast-fitting
        # GBM on that little data would mostly memorize noise, the exact
        # failure mode the module docstring already warns about for a
        # "bigger model." max_depth=2 and a low learning_rate favor
        # underfitting over overfitting on purpose.
        n_estimators=100, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=42,
    )


def _evaluate_on_holdout(pipeline, X_test: list, y_test: list) -> dict:
    from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

    preds = pipeline.predict(X_test)
    probs = pipeline.predict_proba(X_test)[:, 1]
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
    return metrics


def _fit_and_evaluate(X: list, y: list) -> tuple:
    """
    Returns (fitted_ensemble_on_all_data, metrics_dict). metrics_dict's
    top-level accuracy/brier_score/eval_type describe the ENSEMBLE
    (the thing actually deployed/gated by _should_promote below) --
    metrics_dict["logistic_metrics"]/["gbm_metrics"] report each
    sub-model's OWN holdout performance independently alongside it, per
    "compare models using out-of-sample performance" -- so it's always
    visible whether the ensemble is actually earning its extra
    complexity over either model alone, not just asserted.

    Chronological holdout when there's enough data (module docstring);
    train-only metrics (clearly labelled optimistic) otherwise -- same
    split policy as before, just evaluating three models against it
    instead of one.
    """
    from sklearn.metrics import accuracy_score

    n = len(y)
    if n >= MIN_SAMPLES_FOR_HOLDOUT:
        split = int(n * (1 - HOLDOUT_FRACTION))
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        logistic_eval = _new_logistic_pipeline()
        logistic_eval.fit(X_train, y_train)
        gbm_eval = _new_gbm_pipeline()
        gbm_eval.fit(X_train, y_train)
        ensemble_eval = WinProbabilityEnsemble(logistic_eval, gbm_eval)

        metrics = _evaluate_on_holdout(ensemble_eval, X_test, y_test)
        metrics["logistic_metrics"] = _evaluate_on_holdout(logistic_eval, X_test, y_test)
        metrics["gbm_metrics"] = _evaluate_on_holdout(gbm_eval, X_test, y_test)
    else:
        logistic_eval = _new_logistic_pipeline()
        logistic_eval.fit(X, y)
        gbm_eval = _new_gbm_pipeline()
        gbm_eval.fit(X, y)
        ensemble_eval = WinProbabilityEnsemble(logistic_eval, gbm_eval)
        preds = ensemble_eval.predict(X)
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
    final_logistic = _new_logistic_pipeline()
    final_logistic.fit(X, y)
    final_gbm = _new_gbm_pipeline()
    final_gbm.fit(X, y)
    final_ensemble = WinProbabilityEnsemble(final_logistic, final_gbm)
    return final_ensemble, metrics


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
    # `pipeline` is now a WinProbabilityEnsemble of two sub-models, each
    # explaining "which factor matters" a different way -- both reported
    # rather than picking one, per the module's own "AI must explain
    # every signal" principle applied at this layer.
    metrics["feature_coefficients"] = dict(
        zip(feature_names, pipeline.logistic_pipeline.named_steps["clf"].coef_[0].round(4).tolist())
    )
    metrics["feature_importances_gbm"] = dict(
        zip(feature_names, pipeline.gbm_pipeline.feature_importances_.round(4).tolist())
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
