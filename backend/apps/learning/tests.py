from django.test import TestCase

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
        client = APIClient()
        client.force_authenticate(user)
        response = client.get("/api/learning/hypothetical-trades/", {"method": "ema_momentum_scalp"})

        self.assertEqual(response.status_code, 200)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["method"], "ema_momentum_scalp")
