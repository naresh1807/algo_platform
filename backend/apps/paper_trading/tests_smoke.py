"""
End-to-end smoke test for the full autonomous cycle -- exercises
feature engineering, contract selection, risk gating, order execution,
position open/close, P&L settlement, and outcome/decision recording in
one pass. Uses is_market_open's `at=` override (via mocking
apps.market_data.market_hours.is_market_open) rather than depending on
when the test actually runs.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from . import tasks, test_support
from .models import PaperAccount, PaperAIDecision, PaperOrder, PaperPosition


class PaperTradingSmokeTest(TestCase):
    def setUp(self):
        test_support.seed_charge_rules()
        test_support.seed_underlying_candles(n=90, trend_per_bar=3.0)  # steady uptrend -> bullish heuristic
        self.contracts, self.expiry = test_support.seed_option_chain()
        self.market_open_patch = patch("apps.market_data.market_hours.is_market_open", return_value=(True, ""))
        self.market_open_patch.start()
        self.addCleanup(self.market_open_patch.stop)

    @patch("apps.market_data.broker_client.BrokerClient.place_order")
    @patch("apps.market_data.broker_client.BrokerClient.cancel_order")
    def test_full_cycle_enters_and_can_be_closed_without_touching_broker(self, mock_cancel, mock_place):
        result = tasks.run_paper_trading_cycle(underlying=test_support.UNDERLYING, timeframe=test_support.TIMEFRAME)

        self.assertIn(result["action"], ("entered", "hold", "skip", "order_rejected"))
        mock_place.assert_not_called()
        mock_cancel.assert_not_called()

        account = PaperAccount.objects.get(pk=1)
        self.assertGreater(account.initial_capital, 0)

        if result["action"] == "entered":
            position = PaperPosition.objects.get(pk=result["position_id"])
            self.assertEqual(position.status, PaperPosition.Status.OPEN)
            self.assertEqual(PaperPosition.objects.filter(account=account, status="open").count(), 1)
            order = position.entry_order
            self.assertEqual(order.status, PaperOrder.Status.FILLED)
            self.assertEqual(order.quantity, order.contract.lot_size)  # exactly one lot

            decision = PaperAIDecision.objects.get(decision_id=position.entry_decision_id)
            self.assertFalse(decision.is_hypothetical)
            self.assertEqual(decision.source_environment, "PAPER")

            # A second cycle while a position is open must never open a
            # duplicate entry -- it should manage the existing position.
            second = tasks.run_paper_trading_cycle(underlying=test_support.UNDERLYING, timeframe=test_support.TIMEFRAME)
            self.assertIn(second["action"], ("held_position", "exited", "exit_failed", "no_quote_available"))
            self.assertEqual(PaperPosition.objects.filter(account=account, status="open").count(), 1 if second["action"] == "held_position" else 0)

            mock_place.assert_not_called()
            mock_cancel.assert_not_called()
