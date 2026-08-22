"""
Scenarios 26-28: MFE/MAE are calculated correctly; HOLD and SKIP
decisions are recorded; training data contains source_environment=PAPER.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from . import test_support
from .models import Action, PaperAIDecision, get_or_create_account
from .services import execution_engine, outcome_service, position_service


class OutcomeAndDecisionTests(TestCase):
    def setUp(self):
        test_support.seed_charge_rules()
        self.account = get_or_create_account()
        self.contracts, self.expiry = test_support.seed_option_chain()
        self.contract = self.contracts[(list(self.contracts.keys())[0][0], "CE")]
        self.market_open_patch = patch("apps.market_data.market_hours.is_market_open", return_value=(True, ""))
        self.market_open_patch.start()
        self.addCleanup(self.market_open_patch.stop)

    def test_mfe_and_mae_are_calculated_correctly(self):
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position = position_service.open_position(self.account, self.contract, order, stop_loss=Decimal("0.01"), target_price=None)

        entry = position.average_entry_price
        # mark up to a peak, then down to a trough, then exit
        position_service.mark_to_market(position, entry + Decimal("15"))
        position_service.mark_to_market(position, entry - Decimal("3"))
        position_service.mark_to_market(position, entry + Decimal("5"))

        from apps.options.models import OptionChainSnapshot
        exit_price = entry + Decimal("5")
        OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=timezone.now(), ltp=exit_price, open_interest=5000,
            change_in_oi=0, volume=2000, iv=Decimal("15"), bid=exit_price - Decimal("0.2"), ask=exit_price + Decimal("0.2"),
        )
        trade = position_service.close_position(self.account, position, "manual_test")
        result = trade.result

        self.assertEqual(result.mfe, Decimal("15"))
        self.assertEqual(result.mae, Decimal("3"))

    def test_hypothetical_outcome_walk_forward_is_computed_without_creating_an_order(self):
        n, trend, base = 30, 5.0, 24500.0
        test_support.seed_underlying_candles(n=n, base_price=base, trend_per_bar=trend, timeframe="1m")
        from apps.market_data.models import HistoricalData

        first_ts = HistoricalData.objects.filter(symbol=test_support.UNDERLYING, timeframe="1m").order_by("timestamp").first().timestamp
        decision_point = first_ts - timezone.timedelta(minutes=1)
        # seed_underlying_candles climbs FROM (base_price - trend_per_bar*n)
        # UP TO base_price -- the reference price at decision_point (just
        # before the climb starts) is the STARTING price, not base_price.
        entry_reference = base - trend * n

        outcome = outcome_service.compute_hypothetical_outcome(test_support.UNDERLYING, "1m", decision_point, entry_reference, horizon_bars=10)
        self.assertTrue(outcome["available"])
        self.assertGreater(outcome["mfe"], 0)  # uptrend fixture -- price moved favorably

        from .models import PaperOrder

        self.assertEqual(PaperOrder.objects.count(), 0)  # no real order was ever created

    def test_hold_and_skip_decisions_are_recorded_as_hypothetical(self):
        decision = PaperAIDecision.objects.create(
            decision_id=__import__("uuid").uuid4(), timestamp=timezone.now(), symbol=test_support.UNDERLYING,
            feature_snapshot_json={"close": 24500.0}, feature_schema_version="v1", action=Action.HOLD,
            is_hypothetical=True,
        )
        self.assertTrue(decision.is_hypothetical)
        self.assertEqual(decision.source_environment, "PAPER")

        skip_decision = PaperAIDecision.objects.create(
            decision_id=__import__("uuid").uuid4(), timestamp=timezone.now(), symbol=test_support.UNDERLYING,
            contract=self.contract, feature_snapshot_json={"close": 24500.0}, feature_schema_version="v1",
            action=Action.SKIP_SIGNAL, is_hypothetical=True, risk_engine_response_json={"reason": "liquidity too thin"},
        )
        self.assertTrue(skip_decision.is_hypothetical)
        self.assertIn("liquidity", skip_decision.risk_engine_response_json["reason"])

    def test_training_samples_are_written_with_source_environment_paper(self):
        from apps.learning.models import TrainingSample

        from .services import daily_learning_service

        test_support.seed_underlying_candles(n=30, base_price=24500.0, trend_per_bar=5.0, timeframe="5m")
        decision = PaperAIDecision.objects.create(
            decision_id=__import__("uuid").uuid4(), timestamp=timezone.now() - timezone.timedelta(hours=1),
            symbol=test_support.UNDERLYING, feature_snapshot_json={"close": 24500.0}, feature_schema_version="v1",
            action=Action.HOLD, is_hypothetical=True,
            hypothetical_outcome_json={"available": True, "move_pct": 0.05, "mfe": 1, "mae": 1, "final_close": 24501, "bars_forward": 5},
            net_r=0.005,
        )
        created = daily_learning_service.build_training_samples_for_day(timezone.localdate())
        self.assertGreaterEqual(created, 1)
        sample = TrainingSample.objects.get(decision_ref=str(decision.decision_id))
        self.assertEqual(sample.source_environment, "PAPER")
        self.assertEqual(sample.is_hypothetical, True)
