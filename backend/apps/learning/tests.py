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
