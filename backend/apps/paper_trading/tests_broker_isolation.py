"""
Scenarios 1-4: Angel One market data continues to work; placeOrder/
modifyOrder/cancelOrder are never called in paper mode.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from . import tasks, test_support
from .models import PaperAIDecision
from .services import angel_one_guard, contract_selection, market_data_reader


class BrokerIsolationTests(TestCase):
    def setUp(self):
        test_support.seed_charge_rules()
        test_support.seed_underlying_candles(n=90, trend_per_bar=3.0)
        self.contracts, self.expiry = test_support.seed_option_chain()
        self.market_open_patch = patch("apps.market_data.market_hours.is_market_open", return_value=(True, ""))
        self.market_open_patch.start()
        self.addCleanup(self.market_open_patch.stop)

    def test_market_data_still_resolves_underlying_and_contract_quotes(self):
        """Scenario 1: market data (candles, contract quotes) is fully
        available to this app -- read-only reuse of apps.market_data/
        apps.options, no new/broken data path."""
        snapshot = market_data_reader.latest_underlying_snapshot(test_support.UNDERLYING)
        self.assertIsNotNone(snapshot)
        self.assertGreater(snapshot.spot, 0)

        candidate, _ = contract_selection.select_contract(test_support.UNDERLYING, "CE")
        self.assertIsNotNone(candidate)
        self.assertIsNotNone(candidate.quote.ltp)

    @patch("apps.market_data.broker_client.BrokerClient.place_order")
    @patch("apps.market_data.broker_client.BrokerClient.cancel_order")
    def test_place_order_and_cancel_order_never_called_across_a_full_cycle(self, mock_cancel, mock_place):
        """Scenarios 2-3: run several cycles (covering entry, position
        management, and a forced-loss exit) and prove neither method is
        ever called."""
        for _ in range(3):
            tasks.run_paper_trading_cycle(underlying=test_support.UNDERLYING, timeframe=test_support.TIMEFRAME)
        mock_place.assert_not_called()
        mock_cancel.assert_not_called()

    def test_no_modify_order_method_exists_on_broker_client_at_all(self):
        """Scenario: modifyOrder is prohibited -- confirmed the SmartAPI
        client this codebase wraps doesn't even expose a modify_order
        method for this app to accidentally call."""
        from apps.market_data.broker_client import BrokerClient

        self.assertFalse(hasattr(BrokerClient, "modify_order"))

    def test_angel_one_guard_trips_on_a_real_order_call_pattern(self):
        """The static source scanner (services/angel_one_guard.py) must
        actually detect a real violation pattern, not just pass
        vacuously on already-clean code."""
        sample_violation = "from apps.market_data.broker_client import get_broker_client\n"
        matched = any(p.search(sample_violation) for p in angel_one_guard._FORBIDDEN_PATTERNS)
        self.assertTrue(matched, "the guard's own patterns failed to detect a real forbidden import")

        sample_call = "client.place_order(symbol='NIFTY', qty=75)\n"
        matched_call = any(p.search(sample_call) for p in angel_one_guard._FORBIDDEN_PATTERNS)
        self.assertTrue(matched_call, "the guard's own patterns failed to detect a real place_order call")

    def test_angel_one_guard_does_not_false_positive_on_this_apps_own_docstrings(self):
        """Regression guard: apps.options.broker_client (a read-only
        quote client, legitimately used for market data) must never
        trip the scanner just because its name contains 'broker_client'."""
        benign = "from apps.options.broker_client import get_option_chain_client\n"
        matched = any(p.search(benign) for p in angel_one_guard._FORBIDDEN_PATTERNS)
        self.assertFalse(matched, "the guard incorrectly flagged a legitimate read-only quote-client import")

    def test_every_paper_ai_decision_is_source_environment_paper(self):
        tasks.run_paper_trading_cycle(underlying=test_support.UNDERLYING, timeframe=test_support.TIMEFRAME)
        self.assertTrue(PaperAIDecision.objects.exists())
        for decision in PaperAIDecision.objects.all():
            self.assertEqual(decision.source_environment, "PAPER")
