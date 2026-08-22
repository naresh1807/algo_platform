from django.test import SimpleTestCase, TestCase

from .models import DailyReviewNote, DriftEvent, StrategyVersion
from .tasks import check_for_drift


class StrategyVersionTests(TestCase):
    def test_str_shows_active_marker(self):
        version = StrategyVersion.objects.create(version_name="v1", active_flag=True)
        self.assertIn("ACTIVE", str(version))

    def test_params_json_defaults_to_empty_dict(self):
        version = StrategyVersion.objects.create(version_name="v2")
        self.assertEqual(version.params_json, {})


class DailyReviewNoteTests(TestCase):
    def test_defaults_to_unapproved(self):
        note = DailyReviewNote.objects.create(review_date="2026-01-01", summary="test")
        self.assertFalse(note.approved_flag)


class CheckForDriftTests(TestCase):
    def test_skips_with_no_active_strategy_version(self):
        result = check_for_drift()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "no_active_strategy_version")

    def test_skips_with_no_baseline_recorded(self):
        StrategyVersion.objects.create(version_name="v1", active_flag=True, params_json={})
        result = check_for_drift()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "no_baseline_recorded")


class RewardEngineTests(TestCase):
    """manual 11.10/13.9 Reward Engine."""

    def _position(self, entry, stop, qty, pnl):
        from decimal import Decimal
        from types import SimpleNamespace
        return SimpleNamespace(
            qty=qty, entry_price=Decimal(str(entry)), stop_loss=Decimal(str(stop)),
            unrealized_pnl=Decimal(str(pnl)),
        )

    def test_large_profit_scores_20(self):
        from .reward import compute_reward_score
        # risk = 10 * (100-95) = 50; pnl=110 -> R=2.2
        r, score = compute_reward_score(self._position(100, 95, 10, 110))
        self.assertGreaterEqual(r, 2.0)
        self.assertEqual(score, 20)

    def test_large_loss_scores_negative_20(self):
        from .reward import compute_reward_score
        # risk = 10 * 5 = 50; pnl=-60 -> R=-1.2
        r, score = compute_reward_score(self._position(100, 95, 10, -60))
        self.assertLessEqual(r, -1.0)
        self.assertEqual(score, -20)

    def test_zero_risk_distance_returns_none(self):
        from .reward import compute_reward_score
        r, score = compute_reward_score(self._position(100, 100, 10, 5))
        self.assertIsNone(r)
        self.assertIsNone(score)


class ConfidenceBandTests(TestCase):
    """manual 13.10 Decision Confidence."""

    def test_bands(self):
        from .confidence import confidence_band
        self.assertEqual(confidence_band(None), "Unrated")
        self.assertEqual(confidence_band(0.97), "Very Strong")
        self.assertEqual(confidence_band(0.85), "Strong")
        self.assertEqual(confidence_band(0.65), "Average")
        self.assertEqual(confidence_band(0.40), "No Recommendation")


class StrategyRankingTests(TestCase):
    """manual 13.12 Strategy Evaluation."""

    def test_empty_when_no_closed_trades(self):
        from .ranking import rank_strategies
        StrategyVersion.objects.create(version_name="v1", active_flag=True)
        self.assertEqual(rank_strategies(), [])


class ScalpingStrategyIdeaTests(TestCase):
    """
    apps.learning.strategy_methods' three scalping generators -- each
    calls compute_indicators(symbol, timeframe) internally (same style
    as the existing trend_following/mean_reversion/breakout methods),
    so these monkeypatch that call to a crafted ind dict rather than
    seeding real candles, for a fast, deterministic unit test of just
    the decision logic.
    """

    def _patched(self, ind: dict):
        from unittest.mock import patch

        from . import strategy_methods
        return patch.object(strategy_methods, "compute_indicators", return_value=ind)

    def test_ema_momentum_scalp_fires_on_fresh_bullish_momentum(self):
        from .strategy_methods import generate_ema_momentum_scalp_idea

        ind = {
            "close": 100.0, "ema9": 101.0, "ema21": 99.0,
            "macd_hist": 0.5, "macd_hist_prev": 0.2,
            "relative_volume": 1.5, "atr": 2.0,
        }
        with self._patched(ind):
            idea = generate_ema_momentum_scalp_idea("NIFTY")
        self.assertIsNotNone(idea)
        self.assertEqual(idea["entry_price"], 100.0)
        self.assertAlmostEqual(idea["stop_loss"], 100.0 - 0.6 * 2.0)
        self.assertAlmostEqual(idea["target_price"], 100.0 + 1.0 * (100.0 - (100.0 - 0.6 * 2.0)))

    def test_ema_momentum_scalp_none_without_volume_confirmation(self):
        from .strategy_methods import generate_ema_momentum_scalp_idea

        ind = {
            "close": 100.0, "ema9": 101.0, "ema21": 99.0,
            "macd_hist": 0.5, "macd_hist_prev": 0.2,
            "relative_volume": 0.8, "atr": 2.0,  # below 1.2 threshold
        }
        with self._patched(ind):
            idea = generate_ema_momentum_scalp_idea("NIFTY")
        self.assertIsNone(idea)

    def test_rsi_extreme_scalp_fires_on_deep_oversold(self):
        from .strategy_methods import generate_rsi_extreme_scalp_idea

        ind = {"close": 100.0, "rsi": 15.0, "atr": 2.0, "adx": 15.0, "bb_width": 0.02}
        with self._patched(ind):
            idea = generate_rsi_extreme_scalp_idea("NIFTY")
        self.assertIsNotNone(idea)
        self.assertAlmostEqual(idea["stop_loss"], 100.0 - 0.5 * 2.0)

    def test_rsi_extreme_scalp_none_when_only_mildly_oversold(self):
        """
        RSI 28 would fire the existing (5m) mean-reversion method
        (threshold 30) but must NOT fire the scalp version, which needs
        a sharper, more extreme reading (threshold 20).
        """
        from .strategy_methods import generate_rsi_extreme_scalp_idea

        ind = {"close": 100.0, "rsi": 28.0, "atr": 2.0, "adx": 15.0, "bb_width": 0.02}
        with self._patched(ind):
            idea = generate_rsi_extreme_scalp_idea("NIFTY")
        self.assertIsNone(idea)

    def test_sar_volume_burst_scalp_fires_and_stops_at_sar(self):
        from .strategy_methods import generate_sar_volume_burst_scalp_idea

        ind = {"close": 100.0, "sar": 97.0, "relative_volume": 2.0}
        with self._patched(ind):
            idea = generate_sar_volume_burst_scalp_idea("NIFTY")
        self.assertIsNotNone(idea)
        self.assertEqual(idea["stop_loss"], 97.0)  # the SAR level itself, not an ATR multiple

    def test_sar_volume_burst_scalp_none_without_burst(self):
        from .strategy_methods import generate_sar_volume_burst_scalp_idea

        ind = {"close": 100.0, "sar": 97.0, "relative_volume": 1.1}  # below 1.5 threshold
        with self._patched(ind):
            idea = generate_sar_volume_burst_scalp_idea("NIFTY")
        self.assertIsNone(idea)


class RunScalpingStrategyComparisonTests(TestCase):
    def test_skips_outside_market_hours(self):
        from unittest.mock import patch

        from .tasks import run_scalping_strategy_comparison

        with patch("apps.market_data.market_hours.is_market_open", return_value=(False, "market closed")):
            result = run_scalping_strategy_comparison()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "market closed")


class RunScalpingRealExecutionTests(TestCase):
    def test_skips_outside_market_hours(self):
        from unittest.mock import patch

        from .tasks import run_scalping_real_execution

        with patch("apps.market_data.market_hours.is_market_open", return_value=(False, "market closed")):
            result = run_scalping_real_execution()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "market closed")


class EvaluateScalpingStrategyMethodsTests(TestCase):
    """
    apps.learning.tasks._evaluate_method_group, exercised via
    evaluate_scalping_strategy_methods -- specifically that ranking is
    scoped to the scalping group only (a swing-group HypotheticalTrade
    must never affect which scalping method gets active_flag=True).
    """

    def _closed_trade(self, method, symbol, pnl, r_multiple):
        from decimal import Decimal

        from django.utils import timezone

        from .models import HypotheticalTrade
        return HypotheticalTrade.objects.create(
            method=method, symbol=symbol, timeframe="1m",
            entry_price=Decimal("100"), stop_loss=Decimal("99"),
            target_price=Decimal("101"), exit_price=Decimal("101") if pnl > 0 else Decimal("99"),
            pnl=Decimal(str(pnl)), r_multiple=r_multiple, closed_at=timezone.now(),
        )

    def test_insufficient_sample_is_a_documented_noop(self):
        from .tasks import STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL, evaluate_scalping_strategy_methods

        for i in range(STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL - 1):
            self._closed_trade("ema_momentum_scalp", "NIFTY", 10, 1.0)
        result = evaluate_scalping_strategy_methods()
        self.assertEqual(result.get("skipped"), True)
        self.assertEqual(result.get("reason"), "insufficient_sample")

    def test_best_scalping_method_ignores_swing_group_trades(self):
        from .models import ModelRegistry
        from .tasks import STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL, evaluate_scalping_strategy_methods

        # A swing-group method with a much higher win rate -- must NOT
        # be picked as "best" for the scalping group, and must not even
        # appear in this evaluation's stats.
        for i in range(STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL):
            self._closed_trade("trend_following", "NIFTY", 50, 2.0)

        # Two scalping methods, ema_momentum_scalp with the better win rate.
        for i in range(STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL):
            self._closed_trade("ema_momentum_scalp", "NIFTY", 10, 1.0)
        for i in range(STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL):
            self._closed_trade("rsi_extreme_scalp", "NIFTY", -5, -0.5)

        result = evaluate_scalping_strategy_methods()
        self.assertEqual(result["best_method"], "ema_momentum_scalp")
        self.assertNotIn("trend_following", result["stats"])

        active = ModelRegistry.objects.filter(active_flag=True).values_list("model_name", flat=True)
        self.assertIn("strategy_comparison_ema_momentum_scalp", active)
        self.assertNotIn("strategy_comparison_rsi_extreme_scalp", active)


class HypotheticalTradeAPITests(TestCase):
    def test_list_filters_by_method(self):
        from decimal import Decimal

        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group
        from rest_framework.test import APIClient

        from .models import HypotheticalTrade

        HypotheticalTrade.objects.create(
            method="ema_momentum_scalp", symbol="NIFTY", timeframe="1m",
            entry_price=Decimal("100"), stop_loss=Decimal("99"),
        )
        HypotheticalTrade.objects.create(
            method="trend_following", symbol="NIFTY", timeframe="5m",
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
        )

        user = get_user_model().objects.create_user(username="trader1", password="pw")
        user.groups.add(Group.objects.get_or_create(name="Trader")[0])
        client = APIClient()
        client.force_authenticate(user)
        response = client.get("/api/learning/hypothetical-trades/", {"method": "ema_momentum_scalp"})

        self.assertEqual(response.status_code, 200)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["method"], "ema_momentum_scalp")


class RunComparisonCycleIndicatorSnapshotTests(TestCase):
    """
    apps.learning.tasks._run_comparison_cycle -- a freshly-opened
    HypotheticalTrade for a SCALPING-group idea (one that includes "ind"
    in its returned dict, see strategy_methods.py's module docstring)
    must have its ind_* columns populated; a swing-group idea (no "ind"
    key) must leave them unset. method_funcs is passed directly (not the
    real SCALPING_METHOD_FUNCS/METHOD_FUNCS dicts) so each test can supply
    a deterministic fake idea generator -- the "is this a scalping
    method" check in _run_comparison_cycle matches by METHOD NAME against
    the real SCALPING_METHOD_FUNCS, so using a real scalping method name
    here ("ema_momentum_scalp") still exercises that path faithfully.
    """

    def setUp(self):
        from django.utils import timezone

        from apps.market_data.models import HistoricalData

        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="1m", timestamp=timezone.now(),
            open=100, high=101, low=99, close=100, volume=1000, source="test",
        )

    def test_scalping_idea_with_ind_populates_ind_columns(self):
        from .models import HypotheticalTrade
        from .tasks import _run_comparison_cycle

        full_ind = {
            "close": 100.0, "rsi": 55.0, "adx": 22.0, "bb_width": 0.03,
            "relative_volume": 1.4, "atr": 2.0, "macd_hist": 0.3,
            "macd_hist_prev": 0.1, "ema9_slope": 0.5, "ema21_slope": 0.2,
        }

        def fake_idea(symbol, timeframe):
            return {"entry_price": 100.0, "stop_loss": 98.0, "target_price": 103.0, "ind": full_ind}

        _run_comparison_cycle({"ema_momentum_scalp": fake_idea}, "1m", 12, "test")

        trade = HypotheticalTrade.objects.get(method="ema_momentum_scalp", symbol="NIFTY")
        # Stored RAW (0-100 scale) -- same convention as TradingSignal.ind_rsi;
        # rescaling to 0-1 happens later, only in the feature vector
        # (scalp_ml_features.vector_for_hypothetical_trade).
        self.assertAlmostEqual(trade.ind_rsi, 55.0)
        self.assertAlmostEqual(trade.ind_atr_pct, 2.0 / 100.0)

    def test_swing_idea_without_ind_leaves_columns_unset(self):
        from .models import HypotheticalTrade
        from .tasks import _run_comparison_cycle

        def fake_idea(symbol, timeframe):
            return {"entry_price": 100.0, "stop_loss": 98.0, "target_price": 103.0}

        _run_comparison_cycle({"trend_following": fake_idea}, "1m", 48, "test")

        trade = HypotheticalTrade.objects.get(method="trend_following", symbol="NIFTY")
        self.assertIsNone(trade.ind_rsi)


class ScalpWinProbabilityTrainTests(TestCase):
    """apps.learning.scalp_ml_train.train_scalp_win_probability_model."""

    def _closed_scalp(self, method, symbol, pnl, **ind_overrides):
        from decimal import Decimal

        from django.utils import timezone

        from .models import HypotheticalTrade

        ind = dict(
            ind_rsi=50.0, ind_adx=20.0, ind_bb_width=0.02, ind_relative_volume=1.0,
            ind_atr_pct=0.01, ind_macd_hist_pct=0.001, ind_ema9_slope_pct=0.001,
            ind_ema21_slope_pct=0.001,
        )
        ind.update(ind_overrides)
        return HypotheticalTrade.objects.create(
            method=method, symbol=symbol, timeframe="1m",
            entry_price=Decimal("100"), stop_loss=Decimal("99"), target_price=Decimal("101"),
            pnl=Decimal(str(pnl)), closed_at=timezone.now(), **ind,
        )

    def test_insufficient_data_is_a_documented_noop(self):
        from .scalp_ml_train import MIN_TRAINING_SAMPLES, train_scalp_win_probability_model

        for i in range(MIN_TRAINING_SAMPLES - 1):
            self._closed_scalp("ema_momentum_scalp", "NIFTY", 10 if i % 2 == 0 else -10)

        result = train_scalp_win_probability_model()
        self.assertFalse(result["trained"])
        self.assertEqual(result["reason"], "insufficient_data")

    def test_swing_group_trades_are_excluded_from_training(self):
        """
        A swing-group method (trend_following) is not in
        SCALPING_METHOD_FUNCS -- even MIN_TRAINING_SAMPLES worth of its
        closed trades must count as zero towards this model's sample.
        """
        from .scalp_ml_train import MIN_TRAINING_SAMPLES, train_scalp_win_probability_model

        for i in range(MIN_TRAINING_SAMPLES):
            self._closed_scalp("trend_following", "NIFTY", 10 if i % 2 == 0 else -10)

        result = train_scalp_win_probability_model()
        self.assertFalse(result["trained"])
        self.assertEqual(result["reason"], "insufficient_data")
        self.assertEqual(result["sample_count"], 0)

    def test_trains_and_registers_under_its_own_model_name(self):
        from .models import ModelRegistry
        from .scalp_ml_train import MIN_TRAINING_SAMPLES, train_scalp_win_probability_model

        for i in range(MIN_TRAINING_SAMPLES):
            pnl = 10 if i % 2 == 0 else -10
            self._closed_scalp(
                "ema_momentum_scalp", "NIFTY", pnl,
                ind_rsi=70.0 if pnl > 0 else 30.0,
            )

        result = train_scalp_win_probability_model()
        self.assertTrue(result["trained"])
        row = ModelRegistry.objects.get(pk=result["registry_id"])
        self.assertEqual(row.model_name, "scalp_win_probability")
        self.assertTrue(row.active_flag)
        # The real win_probability model must be completely untouched.
        self.assertFalse(ModelRegistry.objects.filter(model_name="win_probability").exists())


class ChronologicalMLBoundaryTests(SimpleTestCase):
    """Forward labels and class composition must respect holdout boundaries."""

    def test_technical_labels_record_the_last_future_candle_used(self):
        import numpy as np

        from .ml_technical import _simulate_labels

        labels, _r_multiples, label_end_indices = _simulate_labels(
            high=np.array([101.0, 102.0, 106.0]),
            low=np.array([99.0, 98.0, 99.0]),
            close=np.array([100.0, 99.0, 105.0]),
            atr=np.array([5.0, 5.0, 5.0]),
            atr_multiplier=1.0,
            max_holding_bars=48,
        )

        self.assertEqual(labels[0], 1.0)
        self.assertEqual(label_end_indices[0], 2.0)

    def test_technical_fit_purges_labels_that_cross_into_holdout(self):
        import numpy as np

        from .ml_technical import _fit_and_evaluate

        n = 20
        source_indices = np.arange(n)
        label_end_indices = source_indices + 1
        X = np.column_stack((source_indices, source_indices % 3)).astype(float)
        y = np.array([i % 2 for i in range(n)])

        model, metrics = _fit_and_evaluate(
            X,
            y,
            ["time", "cycle"],
            label_end_indices=label_end_indices,
            source_indices=source_indices,
        )

        self.assertIsNotNone(model)
        self.assertEqual(metrics["purged_train_rows"], 1)
        self.assertEqual(metrics["train_size"], 14)

    def test_trade_model_returns_no_train_when_earlier_fold_is_single_class(self):
        from .ml_train import _fit_and_evaluate

        # 60 rows activates the chronological holdout. Both classes exist in
        # the full data, but the earlier 45-row training fold has only losses.
        X = [[float(i)] for i in range(60)]
        y = [0] * 45 + [1] * 15

        model, metrics = _fit_and_evaluate(X, y)

        self.assertIsNone(model)
        self.assertEqual(metrics["reason"], "single_class_training_fold")

    def test_technical_model_returns_no_train_for_single_class_training_fold(self):
        import numpy as np

        from .ml_technical import _fit_and_evaluate

        X = np.arange(40, dtype=float).reshape(20, 2)
        y = np.array([0] * 15 + [1] * 5)

        model, metrics = _fit_and_evaluate(X, y, ["a", "b"])

        self.assertIsNone(model)
        self.assertEqual(metrics["reason"], "single_class_training_fold")


class WinProbabilityEnsembleTests(TestCase):
    """
    apps.learning.ml_train.WinProbabilityEnsemble -- averages two
    sub-models' predict_proba output exactly, and is joblib-picklable
    (a real requirement: apps.learning.ml_predict loads it back via
    joblib.load, not just constructs it in-process).
    """

    def test_predict_proba_is_the_exact_average_of_both_submodels(self):
        import numpy as np

        from apps.learning.ml_train import WinProbabilityEnsemble

        class FakeModel:
            def __init__(self, probs):
                self._probs = np.array(probs)

            def predict_proba(self, X):
                return self._probs

        logistic = FakeModel([[0.3, 0.7], [0.6, 0.4]])
        gbm = FakeModel([[0.5, 0.5], [0.2, 0.8]])
        ensemble = WinProbabilityEnsemble(logistic, gbm)

        result = ensemble.predict_proba([[0], [0]])
        np.testing.assert_allclose(result, [[0.4, 0.6], [0.4, 0.6]])

    def test_predict_thresholds_at_half(self):
        import numpy as np

        from apps.learning.ml_train import WinProbabilityEnsemble

        class FakeModel:
            def __init__(self, probs):
                self._probs = np.array(probs)

            def predict_proba(self, X):
                return self._probs

        logistic = FakeModel([[0.9, 0.1], [0.1, 0.9]])
        gbm = FakeModel([[0.9, 0.1], [0.1, 0.9]])
        ensemble = WinProbabilityEnsemble(logistic, gbm)

        preds = ensemble.predict([[0], [0]])
        self.assertEqual(list(preds), [0, 1])

    def test_ensemble_is_joblib_picklable_and_round_trips(self):
        import numpy as np

        from apps.learning.ml_train import (
            WinProbabilityEnsemble, _new_gbm_pipeline, _new_logistic_pipeline,
        )

        X = [[0, 1, 0, 1, 0], [1, 0, 1, 0, 1], [0, 0, 1, 1, 0], [1, 1, 0, 0, 1]] * 10
        y = [0, 1, 0, 1] * 10

        logistic = _new_logistic_pipeline()
        logistic.fit(X, y)
        gbm = _new_gbm_pipeline()
        gbm.fit(X, y)
        ensemble = WinProbabilityEnsemble(logistic, gbm)

        import io

        import joblib

        buffer = io.BytesIO()
        joblib.dump(ensemble, buffer)
        buffer.seek(0)
        reloaded = joblib.load(buffer)

        original_probs = ensemble.predict_proba(X)
        reloaded_probs = reloaded.predict_proba(X)
        np.testing.assert_allclose(original_probs, reloaded_probs)


class ScalpWinProbabilityPredictTests(TestCase):
    """apps.learning.scalp_ml_predict.predict_scalp_win_probability."""

    def _trade(self, **overrides):
        from decimal import Decimal

        from .models import HypotheticalTrade

        fields = dict(
            method="ema_momentum_scalp", symbol="NIFTY", timeframe="1m",
            entry_price=Decimal("100"), stop_loss=Decimal("99"),
            ind_rsi=70.0, ind_adx=20.0, ind_bb_width=0.02, ind_relative_volume=1.0,
            ind_atr_pct=0.01, ind_macd_hist_pct=0.001, ind_ema9_slope_pct=0.001,
            ind_ema21_slope_pct=0.001,
        )
        fields.update(overrides)
        return HypotheticalTrade(**fields)

    def test_returns_none_with_no_active_model(self):
        from .scalp_ml_predict import predict_scalp_win_probability

        self.assertIsNone(predict_scalp_win_probability(self._trade()))

    def test_returns_a_probability_once_a_model_is_trained(self):
        from decimal import Decimal

        from django.utils import timezone

        from .models import HypotheticalTrade
        from .scalp_ml_predict import predict_scalp_win_probability
        from .scalp_ml_train import MIN_TRAINING_SAMPLES, train_scalp_win_probability_model

        for i in range(MIN_TRAINING_SAMPLES):
            pnl = 10 if i % 2 == 0 else -10
            HypotheticalTrade.objects.create(
                method="ema_momentum_scalp", symbol="NIFTY", timeframe="1m",
                entry_price=Decimal("100"), stop_loss=Decimal("99"), target_price=Decimal("101"),
                pnl=Decimal(str(pnl)), closed_at=timezone.now(),
                ind_rsi=70.0 if pnl > 0 else 30.0, ind_adx=20.0, ind_bb_width=0.02,
                ind_relative_volume=1.0, ind_atr_pct=0.01, ind_macd_hist_pct=0.001,
                ind_ema9_slope_pct=0.001, ind_ema21_slope_pct=0.001,
            )
        train_scalp_win_probability_model()

        probability = predict_scalp_win_probability(self._trade())
        self.assertIsNotNone(probability)
        self.assertTrue(0.0 <= probability <= 1.0)


class EvaluateAndOpenScalpTests(TestCase):
    """
    apps.learning.scalp_execution.evaluate_and_open_scalp -- the REAL
    (paper-mode) option-position path for the scalping methods, additional
    to (never touching) the isolated HypotheticalTrade comparison. Mocks
    the same upstream gates apps.options.tests.
    EvaluateIndexDirectionTradeApprovalTests mocks for the equivalent
    index_direction_strategy wiring test -- each gate has its own coverage
    elsewhere; this only checks the wiring from "idea fired and cleared
    every gate" to "a real OpenPosition exists".
    """

    def _fake_idea(self, entry=100.0, stop=94.0, target=106.0):
        full_ind = {
            "close": entry, "ema9": entry + 1, "ema21": entry - 1,
            "ema9_slope": 0.5, "ema21_slope": 0.1, "sar": stop,
            "macd": 1.0, "macd_signal": 0.5, "macd_hist": 0.3, "macd_hist_prev": 0.1,
            "rsi": 60.0, "relative_volume": 1.5, "adx": 30.0, "bb_width": 0.02, "atr": entry - stop,
        }
        return {"entry_price": entry, "stop_loss": stop, "target_price": target, "ind": full_ind}

    def test_returns_none_when_idea_generator_returns_none(self):
        from .models import HypotheticalTrade
        from .scalp_execution import evaluate_and_open_scalp

        result = evaluate_and_open_scalp("ema_momentum_scalp", lambda s, tf: None, "NIFTY")

        self.assertIsNone(result)
        self.assertEqual(HypotheticalTrade.objects.count(), 0)

    def test_no_contract_resolved_creates_no_trade_and_no_position(self):
        from unittest.mock import patch

        from apps.execution.models import OpenPosition
        from apps.signals.models import TradingSignal
        from common.constants import SignalStatus

        from .scalp_execution import evaluate_and_open_scalp

        with patch("apps.options.signals_engine.nearest_expiry", return_value=None):
            signal = evaluate_and_open_scalp(
                "ema_momentum_scalp", lambda s, tf: self._fake_idea(), "NIFTY",
            )

        self.assertIsInstance(signal, TradingSignal)
        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertIn("No options contracts synced", signal.reason)
        self.assertEqual(OpenPosition.objects.count(), 0)

    def test_lot_rounding_rejects_when_under_one_lot(self):
        from datetime import date, timedelta
        from unittest.mock import patch

        from apps.execution.models import OpenPosition
        from apps.options.models import OptionContract
        from apps.risk.engine import RiskDecision
        from common.constants import SignalStatus

        from .scalp_execution import evaluate_and_open_scalp

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=100,
            option_type="CE", symbol_token="tok_ce_100", tradingsymbol="NIFTY100CE",
            lot_size=75,  # bigger than the mocked position_size below
        )
        suggestion = {
            "suggested": {"contract_id": contract.pk, "strike": 100.0, "ltp": 5.0, "delta": 0.5},
            "reason": "test suggestion",
        }
        risk_decision = RiskDecision(approved=True, risk_score=1.0, reasons=[], position_size=10)

        with patch("apps.options.signals_engine.nearest_expiry", return_value=date.today() + timedelta(days=7)), \
             patch("apps.options.strike_selector.suggest_best_strike", return_value=suggestion), \
             patch("apps.risk.engine.check_pre_trade", return_value=risk_decision):
            signal = evaluate_and_open_scalp(
                "ema_momentum_scalp", lambda s, tf: self._fake_idea(), "NIFTY",
            )

        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertIn("under one lot", signal.reason)
        self.assertEqual(OpenPosition.objects.count(), 0)

    def test_approved_case_opens_a_real_position(self):
        from datetime import date, timedelta
        from decimal import Decimal
        from unittest.mock import patch

        from django.utils import timezone

        from apps.execution.models import OpenPosition
        from apps.options.models import OptionChainSnapshot, OptionContract
        from apps.risk.engine import RiskDecision
        from common.constants import PositionSide, SignalStatus

        from .scalp_execution import evaluate_and_open_scalp

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=100,
            option_type="CE", symbol_token="tok_ce_100b", tradingsymbol="NIFTY100CE",
            lot_size=25,
        )
        # apps.risk.engine.validate_signal_for_execution's execution-time
        # liquidity check (real safety hardening) requires a fresh
        # OptionChainSnapshot with a tight bid/ask spread and real OI for
        # any option_contract signal -- without one it rejects with "No
        # option quote snapshot exists for execution-time liquidity
        # validation," which this test's fixture never provided.
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=timezone.now(), ltp=Decimal("5.0"),
            bid=Decimal("4.9"), ask=Decimal("5.0"), open_interest=1000,
            change_in_oi=0, volume=100,
        )
        suggestion = {
            "suggested": {"contract_id": contract.pk, "strike": 100.0, "ltp": 5.0, "delta": 0.5},
            "reason": "test suggestion",
        }
        risk_decision = RiskDecision(approved=True, risk_score=1.0, reasons=[], position_size=50)

        # apps.risk.engine.validate_signal_for_execution's execution-time
        # checks include a real is_market_open() veto -- without mocking
        # it, this test only passes when actually run during NSE hours
        # (09:15-15:30 IST), a real, discovered flakiness unrelated to
        # what this test is meant to verify (that an approved signal
        # opens a real position).
        with patch("apps.options.signals_engine.nearest_expiry", return_value=date.today() + timedelta(days=7)), \
             patch("apps.options.strike_selector.suggest_best_strike", return_value=suggestion), \
             patch("apps.risk.engine.check_pre_trade", return_value=risk_decision), \
             patch("apps.market_data.market_hours.is_market_open", return_value=(True, "")):
            signal = evaluate_and_open_scalp(
                "ema_momentum_scalp", lambda s, tf: self._fake_idea(), "NIFTY",
            )

        # open_position_from_signal marks it EXECUTED once the real
        # position opens (it was APPROVED right up until that call) --
        # but it does so on ITS OWN freshly select_for_update()-loaded
        # copy of the signal, not the object reference this test/
        # evaluate_and_open_scalp already holds, so that mutation is
        # only visible after a real refresh from the DB.
        signal.refresh_from_db()
        self.assertEqual(signal.status, SignalStatus.EXECUTED)
        self.assertEqual(signal.option_contract_id, contract.pk)
        self.assertEqual(signal.position_size, 50)  # 2 lots x 25

        position = OpenPosition.objects.get(signal=signal)
        self.assertEqual(position.side, PositionSide.LONG)
        self.assertEqual(position.option_contract_id, contract.pk)
        self.assertEqual(position.symbol, "NIFTY100CE")

    def test_never_creates_a_hypothetical_trade(self):
        """
        This path must stay fully additive to, never touching,
        _run_comparison_cycle's own isolated HypotheticalTrade rows.
        """
        from datetime import date, timedelta
        from unittest.mock import patch

        from apps.options.models import OptionContract
        from apps.risk.engine import RiskDecision

        from .models import HypotheticalTrade
        from .scalp_execution import evaluate_and_open_scalp

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=100,
            option_type="CE", symbol_token="tok_ce_100c", tradingsymbol="NIFTY100CE",
            lot_size=25,
        )
        suggestion = {
            "suggested": {"contract_id": contract.pk, "strike": 100.0, "ltp": 5.0, "delta": 0.5},
            "reason": "test suggestion",
        }
        risk_decision = RiskDecision(approved=True, risk_score=1.0, reasons=[], position_size=50)

        with patch("apps.options.signals_engine.nearest_expiry", return_value=date.today() + timedelta(days=7)), \
             patch("apps.options.strike_selector.suggest_best_strike", return_value=suggestion), \
             patch("apps.risk.engine.check_pre_trade", return_value=risk_decision):
            evaluate_and_open_scalp("ema_momentum_scalp", lambda s, tf: self._fake_idea(), "NIFTY")

        self.assertEqual(HypotheticalTrade.objects.count(), 0)

    def test_low_ml_confidence_rejects_before_opening_a_real_position(self):
        """
        Real bug fix: this path used to skip straight from "risk-approved"
        to a real paper OpenPosition with no confidence check at all --
        exactly how a rule-approved setup with e.g. 28% ML win-probability
        could end up with status=Opened. Same
        apps.learning.ml_predict.should_reject_for_low_confidence gate
        apps.signals.engine.generate_signal and apps.options.
        index_direction_strategy already apply must now also apply here.
        """
        from datetime import date, timedelta
        from unittest.mock import patch

        from apps.execution.models import OpenPosition
        from apps.options.models import OptionContract
        from apps.risk.engine import RiskDecision
        from common.constants import SignalStatus

        from .scalp_execution import evaluate_and_open_scalp

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=100,
            option_type="CE", symbol_token="tok_ce_100d", tradingsymbol="NIFTY100CE",
            lot_size=25,
        )
        suggestion = {
            "suggested": {"contract_id": contract.pk, "strike": 100.0, "ltp": 5.0, "delta": 0.5},
            "reason": "test suggestion",
        }
        risk_decision = RiskDecision(approved=True, risk_score=1.0, reasons=[], position_size=50)

        with patch("apps.options.signals_engine.nearest_expiry", return_value=date.today() + timedelta(days=7)), \
             patch("apps.options.strike_selector.suggest_best_strike", return_value=suggestion), \
             patch("apps.risk.engine.check_pre_trade", return_value=risk_decision), \
             patch("apps.learning.ml_predict.predict_win_probability", return_value=0.28):
            signal = evaluate_and_open_scalp(
                "sar_volume_burst_scalp", lambda s, tf: self._fake_idea(), "BANKNIFTY",
            )

        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertEqual(signal.rejection_stage, "ml_confidence")
        self.assertEqual(signal.ml_win_probability, 0.28)
        self.assertIn("28%", signal.reason)
        self.assertIn("No real order placed", signal.reason)
        self.assertEqual(OpenPosition.objects.count(), 0)

    def test_high_ml_confidence_still_opens_a_real_position(self):
        from datetime import date, timedelta
        from decimal import Decimal
        from unittest.mock import patch

        from django.utils import timezone

        from apps.execution.models import OpenPosition
        from apps.options.models import OptionChainSnapshot, OptionContract
        from apps.risk.engine import RiskDecision
        from common.constants import SignalStatus

        from .scalp_execution import evaluate_and_open_scalp

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=100,
            option_type="CE", symbol_token="tok_ce_100e", tradingsymbol="NIFTY100CE",
            lot_size=25,
        )
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=timezone.now(), ltp=Decimal("5.0"),
            bid=Decimal("4.9"), ask=Decimal("5.0"), open_interest=1000,
            change_in_oi=0, volume=100,
        )
        suggestion = {
            "suggested": {"contract_id": contract.pk, "strike": 100.0, "ltp": 5.0, "delta": 0.5},
            "reason": "test suggestion",
        }
        risk_decision = RiskDecision(approved=True, risk_score=1.0, reasons=[], position_size=50)

        with patch("apps.options.signals_engine.nearest_expiry", return_value=date.today() + timedelta(days=7)), \
             patch("apps.options.strike_selector.suggest_best_strike", return_value=suggestion), \
             patch("apps.risk.engine.check_pre_trade", return_value=risk_decision), \
             patch("apps.market_data.market_hours.is_market_open", return_value=(True, "")), \
             patch("apps.learning.ml_predict.predict_win_probability", return_value=0.72):
            signal = evaluate_and_open_scalp(
                "ema_momentum_scalp", lambda s, tf: self._fake_idea(), "NIFTY",
            )

        self.assertEqual(signal.ml_win_probability, 0.72)
        signal.refresh_from_db()
        self.assertEqual(signal.status, SignalStatus.EXECUTED)
        self.assertEqual(OpenPosition.objects.count(), 1)
