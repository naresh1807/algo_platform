"""
Scenarios 29-33: no future leakage in the walk-forward split; a failed
challenger never replaces the champion; a promoted model preserves its
full preprocessing bundle; restarting services doesn't reset the paper
account or learning history; switching execution configuration doesn't
reset the champion model.
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.learning.models import ModelRegistry, TrainingSample

from . import test_support
from .models import get_or_create_account
from .services import model_evaluation_service, model_promotion_service, model_training_service


def _seed_training_samples(n: int = 80, model_name: str = "paper_trading_policy", seed_random: bool = True):
    if seed_random:
        random.seed(42)
    base = timezone.now() - timedelta(days=10)
    samples = []
    for i in range(n):
        drift = (i / n) - 0.5  # a mild, learnable signal drifting from 0 to 1
        f1 = drift + random.uniform(-0.1, 0.1)
        label = 1 if drift + random.uniform(-0.3, 0.3) > 0 else 0
        samples.append(TrainingSample(
            source_environment=TrainingSample.SourceEnvironment.PAPER, model_name=model_name,
            decision_ref=f"seed-{i}", symbol="NIFTY", timestamp=base + timedelta(minutes=5 * i),
            feature_schema_version="v1", feature_vector_json={"f1": f1, "regime": "trending" if i % 2 == 0 else "sideways"},
            label=label, net_r=(0.5 if label else -0.5), is_hypothetical=False,
        ))
    TrainingSample.objects.bulk_create(samples)
    return samples


class MLPipelineTests(TestCase):
    def test_no_future_leakage_in_the_chronological_gap_split(self):
        _seed_training_samples(n=80)
        result = model_training_service.train_challenger()
        self.assertTrue(result["trained"], result.get("reason"))

        rows = model_training_service.build_dataset()
        n = len(rows)
        holdout_size = max(10, int(n * 0.25))
        split = n - holdout_size
        train_end = max(0, split - model_training_service.GAP_SAMPLES)

        train_rows = rows[:train_end]
        holdout_rows = rows[split:]
        # Every training row's timestamp must be strictly earlier than
        # every holdout row's timestamp, with the documented gap between
        # them -- this is what "no future leakage" actually means: the
        # model never trains on data chronologically adjacent to (or
        # after) what it's evaluated against.
        self.assertTrue(train_rows)
        self.assertTrue(holdout_rows)
        self.assertLess(max(r.timestamp for r in train_rows), min(r.timestamp for r in holdout_rows))
        gap_rows = rows[train_end:split]
        self.assertEqual(len(gap_rows), model_training_service.GAP_SAMPLES)

    def test_a_failed_challenger_does_not_replace_the_champion(self):
        champion = ModelRegistry.objects.create(
            model_name="paper_trading_policy", model_version="champion_v1", artifact_path="/nonexistent/champion.joblib",
            metrics_json={"eval_type": "chronological_holdout", "accuracy": 0.52, "holdout_size": 50},
            active_flag=True,
        )
        # Challenger clears the accuracy-regression gate (close to the
        # champion's own accuracy) but is deliberately unprofitable --
        # this exercises THIS subsystem's OWN additional gates (not just
        # the accuracy gate it shares with apps.learning.ml_train). A
        # negative-expectancy/low-profit-factor challenger fails on
        # whichever of those two correlated gates evaluate_promotion
        # checks first; either is a correct rejection.
        performance = {"sample_count": 20, "expectancy_r": -0.5, "profit_factor": 0.3, "max_drawdown_r": 1.0}
        promoted, reason = model_promotion_service.evaluate_promotion(
            model_name="paper_trading_policy",
            challenger_metrics={"eval_type": "chronological_holdout", "accuracy": 0.51, "holdout_size": 20},
            performance=performance, regimes={"trending"},
        )
        self.assertFalse(promoted)
        self.assertTrue(
            "profit_factor" in reason or "expectancy" in reason,
            f"expected rejection for unprofitable performance, got: {reason}",
        )

        champion.refresh_from_db()
        self.assertTrue(champion.active_flag)
        self.assertEqual(champion.model_version, "champion_v1")

    def test_promoted_model_preserves_its_full_preprocessing_bundle(self):
        import joblib

        from apps.learning.ml_train import WinProbabilityEnsemble

        _seed_training_samples(n=80)
        training_result = model_training_service.train_challenger()
        self.assertTrue(training_result["trained"])

        performance = model_evaluation_service.evaluate_holdout_performance(training_result["holdout_rows"])
        regimes = model_evaluation_service.regimes_represented(training_result["holdout_rows"])
        # Force a pass regardless of the randomly-generated fixture's
        # actual holdout performance -- this test is about ARTIFACT
        # INTEGRITY, not about whether the toy dataset happens to be
        # profitable.
        performance = {**performance, "expectancy_r": 1.0, "profit_factor": 2.0, "max_drawdown_r": 0.1, "sample_count": max(performance["sample_count"], 10)}
        promotion = model_promotion_service.promote_or_reject(
            model_name="paper_trading_policy", ensemble=training_result["ensemble"],
            metrics=training_result["metrics"], performance=performance, regimes=regimes or {"trending"},
        )
        self.assertTrue(promotion["promoted"], promotion["reason"])

        registry_row = ModelRegistry.objects.get(model_name="paper_trading_policy", model_version=promotion["model_version"])
        loaded = joblib.load(registry_row.artifact_path)
        self.assertIsInstance(loaded, WinProbabilityEnsemble)
        self.assertTrue(hasattr(loaded.logistic_pipeline, "named_steps"))
        self.assertIn("scaler", loaded.logistic_pipeline.named_steps)
        self.assertIn("clf", loaded.logistic_pipeline.named_steps)
        self.assertTrue(hasattr(loaded.gbm_pipeline, "predict_proba"))
        # The exact ordered feature-column list is preserved alongside
        # the artifact -- required for inference to build the same
        # vector shape the model was trained on (apps.learning.
        # ml_features's own documented convention).
        self.assertIn("feature_columns", registry_row.metrics_json)
        vector = [0.1] * len(registry_row.metrics_json["feature_columns"])
        proba = loaded.predict_proba([vector])
        self.assertEqual(len(proba[0]), 2)

    def test_restarting_services_does_not_reset_the_paper_account_or_learning_history(self):
        account = get_or_create_account()
        account.available_cash = Decimal("87654.32")
        account.realized_pnl = Decimal("-2345.68")
        account.save(update_fields=["available_cash", "realized_pnl"])

        ModelRegistry.objects.create(
            model_name="paper_trading_policy", model_version="persisted_v1", artifact_path="/tmp/persisted.joblib",
            metrics_json={"accuracy": 0.6}, active_flag=True,
        )

        # "Restart" == the singleton accessor is called again from a
        # fresh code path, exactly as a newly-started Celery worker
        # process would -- it must NEVER re-seed from
        # settings.INITIAL_PAPER_CAPITAL once a row already exists.
        reloaded_account = get_or_create_account()
        self.assertEqual(reloaded_account.available_cash, Decimal("87654.32"))
        self.assertEqual(reloaded_account.realized_pnl, Decimal("-2345.68"))

        self.assertTrue(ModelRegistry.objects.filter(model_name="paper_trading_policy", model_version="persisted_v1", active_flag=True).exists())

    def test_switching_execution_mode_setting_does_not_touch_the_paper_trading_champion(self):
        """apps.execution.models.ExecutionModeSetting (the OTHER, shared-
        table execution pipeline's paper/live toggle) is a completely
        separate concern from apps.paper_trading's own champion model --
        flipping it must never touch apps.learning.ModelRegistry rows
        for 'paper_trading_policy'."""
        from apps.execution.models import ExecutionModeSetting, get_execution_mode

        champion = ModelRegistry.objects.create(
            model_name="paper_trading_policy", model_version="stable_v1", artifact_path="/tmp/stable.joblib",
            metrics_json={"accuracy": 0.7}, active_flag=True,
        )

        self.assertEqual(get_execution_mode(), "paper")
        ExecutionModeSetting.objects.filter(pk=1).update(mode="live") if ExecutionModeSetting.objects.filter(pk=1).exists() else ExecutionModeSetting.objects.create(pk=1, mode="live")
        self.assertEqual(get_execution_mode(), "live")
        ExecutionModeSetting.objects.filter(pk=1).update(mode="paper")

        champion.refresh_from_db()
        self.assertTrue(champion.active_flag)
        self.assertEqual(champion.model_version, "stable_v1")
