from datetime import timedelta
from decimal import Decimal
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from .models import RealizedTradeFeedback, TradeFeedbackRun
from .trade_feedback import numeric_entry_features, record_feedback, retrain_from_taken_trades
from unittest.mock import patch


class FeedbackFeatureTests(SimpleTestCase):
    def test_outcome_fields_never_become_entry_features(self):
        result=numeric_entry_features({"rsi":50,"net_pnl":100,"exit_price":101,"mfe":50,"atr":float("nan"),"adx":True})
        self.assertEqual(result,{"ind_rsi":50.0})


class FeedbackTests(TestCase):
    def record(self,net=Decimal("20"),source_id=1,risk=Decimal("10")):
        now=timezone.now()
        return record_feedback("test",source_id,"NIFTY","test",now-timedelta(minutes=5),now,{"rsi":50},net,Decimal("2"),risk)

    def test_duplicate_trade_is_not_learned_twice(self):
        self.assertTrue(self.record())
        self.assertFalse(self.record())
        self.assertEqual(RealizedTradeFeedback.objects.count(),1)

    def test_net_return_and_loss_labels(self):
        self.record()
        self.record(net=Decimal("-5"),source_id=2)
        rows=list(RealizedTradeFeedback.objects.order_by("source_id"))
        self.assertEqual(rows[0].net_r,2)
        self.assertTrue(rows[0].profitable)
        self.assertEqual(rows[1].net_r,-.5)
        self.assertFalse(rows[1].profitable)

    def test_invalid_risk_is_rejected(self):
        self.assertFalse(self.record(risk=Decimal("0")))
        self.assertFalse(RealizedTradeFeedback.objects.exists())

    @patch("apps.learning.trade_feedback.collect_trade_feedback")
    def test_insufficient_trades_does_not_train_or_promote(self,collect):
        self.record()
        result=retrain_from_taken_trades()
        self.assertFalse(result["trained"])
        self.assertEqual(result["reason"],"insufficient_taken_trade_history")
        self.assertEqual(TradeFeedbackRun.objects.count(),1)
