from django.test import TestCase

from .models import FeedHealthCheck


class FeedHealthCheckTests(TestCase):
    def test_str_shows_ok_when_healthy(self):
        check = FeedHealthCheck.objects.create(source="angel_one", is_healthy=True, latency_ms=120)
        self.assertIn("OK", str(check))

    def test_str_shows_down_when_unhealthy(self):
        check = FeedHealthCheck.objects.create(source="angel_one", is_healthy=False, detail="timeout")
        self.assertIn("DOWN", str(check))

    def test_latest_check_ordering(self):
        first = FeedHealthCheck.objects.create(source="angel_one", is_healthy=True)
        second = FeedHealthCheck.objects.create(source="angel_one", is_healthy=False)
        latest = FeedHealthCheck.objects.order_by("-checked_at").first()
        self.assertEqual(latest.pk, second.pk)


class PriceAlertTaskTests(TestCase):
    """apps.monitoring.tasks.check_price_alerts."""

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.market_data.models import HistoricalData
        from .models import PriceAlert

        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24800, high=24950, low=24780, close=24900, volume=1000, source="test",
        )
        self.above_alert = PriceAlert.objects.create(symbol="NIFTY", condition="above", target_price=24800)
        self.below_alert = PriceAlert.objects.create(symbol="NIFTY", condition="below", target_price=24800)
        self.not_reached_alert = PriceAlert.objects.create(symbol="NIFTY", condition="above", target_price=25500)

    def test_triggers_alert_whose_condition_is_met(self):
        from .tasks import check_price_alerts

        result = check_price_alerts()
        self.above_alert.refresh_from_db()
        self.not_reached_alert.refresh_from_db()

        self.assertEqual(result["triggered"], 1)
        self.assertFalse(self.above_alert.active)
        self.assertIsNotNone(self.above_alert.triggered_at)
        self.assertTrue(self.not_reached_alert.active)  # 25500 not reached yet

    def test_does_not_trigger_alert_whose_condition_is_not_met(self):
        from .tasks import check_price_alerts

        check_price_alerts()
        self.below_alert.refresh_from_db()
        self.assertTrue(self.below_alert.active)  # price (24900) is above target (24800), not below
