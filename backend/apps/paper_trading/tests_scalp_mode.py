"""
Scalp-mode coverage: ai_signal_service.decide_scalp_direction/
evaluate_scalp_entry and tasks.run_paper_trading_scalp_cycle -- the
1-minute sibling of the swing cycle, sourcing candidate entries from
apps.learning.strategy_methods.SCALPING_METHOD_FUNCS (reused, not
re-implemented) instead of the swing heuristic. Patches
SCALPING_METHOD_FUNCS directly (same style as apps.learning.tests.
EvaluateAndOpenScalpTests' own _fake_idea fixture) for deterministic,
fast tests rather than depending on real indicator math over synthetic
candles to happen to cross the swing heuristic's thresholds.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from . import tasks, test_support
from .models import Action, PaperAIDecision, PaperAccount, PaperPosition, get_or_create_account
from .services import ai_signal_service


def _fake_idea(entry=100.0, stop=94.0, target=106.0):
    full_ind = {
        "close": entry, "ema9": entry - 1, "ema21": entry - 2,
        "ema9_slope": 0.5, "ema21_slope": 0.1, "sar": stop,
        "macd": 1.0, "macd_signal": 0.5, "macd_hist": 0.3, "macd_hist_prev": 0.1,
        "rsi": 60.0, "relative_volume": 1.5, "adx": 30.0, "bb_width": 0.02, "atr": entry - stop,
    }
    return {"entry_price": entry, "stop_loss": stop, "target_price": target, "ind": full_ind}


class ScalpModeTests(TestCase):
    def setUp(self):
        test_support.seed_charge_rules()
        test_support.seed_underlying_candles(n=90, timeframe="1m")
        self.contracts, self.expiry = test_support.seed_option_chain()
        self.market_open_patch = patch("apps.market_data.market_hours.is_market_open", return_value=(True, ""))
        self.market_open_patch.start()
        self.addCleanup(self.market_open_patch.stop)

    def test_holds_when_no_scalp_method_fires(self):
        no_ideas = {
            "ema_momentum_scalp": lambda s, tf: None,
            "rsi_extreme_scalp": lambda s, tf: None,
            "sar_volume_burst_scalp": lambda s, tf: None,
        }
        with patch.dict("apps.learning.strategy_methods.SCALPING_METHOD_FUNCS", no_ideas, clear=True):
            result = ai_signal_service.decide_scalp_direction(test_support.UNDERLYING)

        self.assertEqual(result.action, Action.HOLD)

    def test_fires_ce_from_heuristic_when_a_scalp_method_fires_and_no_champion_model_exists(self):
        one_idea = {"sar_volume_burst_scalp": lambda s, tf: _fake_idea()}
        with patch.dict("apps.learning.strategy_methods.SCALPING_METHOD_FUNCS", one_idea, clear=True):
            result = ai_signal_service.decide_scalp_direction(test_support.UNDERLYING)

        self.assertEqual(result.action, Action.BUY_CE_ONE_LOT)
        self.assertTrue(0.0 <= result.confidence <= 1.0)
        self.assertIn("sar_volume_burst_scalp", result.reason)
        # (100 - 94) / 100 -- the fired idea's own underlying-level risk ratio.
        self.assertIsNotNone(result.stop_distance_ratio)
        self.assertAlmostEqual(float(result.stop_distance_ratio), 0.06, places=4)

    def test_evaluate_scalp_entry_selects_a_contract_and_sizes_a_tight_stop_below_target(self):
        account = get_or_create_account()
        one_idea = {"ema_momentum_scalp": lambda s, tf: _fake_idea()}
        with patch.dict("apps.learning.strategy_methods.SCALPING_METHOD_FUNCS", one_idea, clear=True):
            result = ai_signal_service.evaluate_scalp_entry(account, test_support.UNDERLYING)

        self.assertEqual(result.action, Action.BUY_CE_ONE_LOT)
        self.assertIsNotNone(result.contract_candidate)
        self.assertEqual(result.contract_candidate.contract.option_type, "CE")
        self.assertGreater(result.selected_stop, 0)
        self.assertLess(result.selected_stop, result.selected_target)

    @patch("apps.market_data.broker_client.BrokerClient.place_order")
    @patch("apps.market_data.broker_client.BrokerClient.cancel_order")
    def test_run_paper_trading_scalp_cycle_opens_a_real_paper_position_without_touching_broker(self, mock_cancel, mock_place):
        one_idea = {"rsi_extreme_scalp": lambda s, tf: _fake_idea()}
        with patch.dict("apps.learning.strategy_methods.SCALPING_METHOD_FUNCS", one_idea, clear=True):
            result = tasks.run_paper_trading_scalp_cycle(underlying=test_support.UNDERLYING)

        self.assertEqual(result["action"], "entered")
        mock_place.assert_not_called()
        mock_cancel.assert_not_called()

        position = PaperPosition.objects.get(pk=result["position_id"])
        self.assertEqual(position.status, PaperPosition.Status.OPEN)
        order = position.entry_order
        self.assertEqual(order.quantity, order.contract.lot_size)  # exactly one lot

        decision = PaperAIDecision.objects.get(decision_id=position.entry_decision_id)
        self.assertFalse(decision.is_hypothetical)
        self.assertEqual(decision.action, Action.BUY_CE_ONE_LOT)
        self.assertEqual(decision.source_environment, "PAPER")

    @patch("apps.market_data.broker_client.BrokerClient.place_order")
    @patch("apps.market_data.broker_client.BrokerClient.cancel_order")
    def test_scalp_and_swing_cycles_share_the_single_open_position_slot(self, mock_cancel, mock_place):
        one_idea = {"ema_momentum_scalp": lambda s, tf: _fake_idea()}
        with patch.dict("apps.learning.strategy_methods.SCALPING_METHOD_FUNCS", one_idea, clear=True):
            scalp_result = tasks.run_paper_trading_scalp_cycle(underlying=test_support.UNDERLYING)

        self.assertEqual(scalp_result["action"], "entered")

        # A swing-cycle tick while the scalp cycle's position is still
        # open must manage it (or find it already closed by the same
        # tick's exit check), never open a second, competing position.
        swing_result = tasks.run_paper_trading_cycle(underlying=test_support.UNDERLYING, timeframe=test_support.TIMEFRAME)
        self.assertIn(swing_result["action"], ("held_position", "exited", "exit_failed", "no_quote_available"))

        account = PaperAccount.objects.get(pk=1)
        self.assertLessEqual(PaperPosition.objects.filter(account=account, status="open").count(), 1)
        mock_place.assert_not_called()
        mock_cancel.assert_not_called()
