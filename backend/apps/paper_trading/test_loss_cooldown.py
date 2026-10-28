from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from .services.paper_risk_engine import consecutive_losses_cooldown_active


@override_settings(AI_PAPER_MAX_CONSECUTIVE_LOSSES=3, AI_PAPER_COOLDOWN_MINUTES=15)
class LossCooldownTests(SimpleTestCase):
    def check(self, losses, age):
        now = timezone.now()
        with patch("apps.paper_trading.services.paper_risk_engine.consecutive_losses", return_value=losses), \
             patch("apps.paper_trading.services.paper_risk_engine.PaperTrade.objects") as trades, \
             patch("apps.paper_trading.services.paper_risk_engine.timezone.now", return_value=now):
            trades.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(
                closed_at=now-timedelta(minutes=age))
            return consecutive_losses_cooldown_active(SimpleNamespace(pk=1))

    def test_recent_loss_streak_blocks(self):
        self.assertTrue(self.check(3, 10))

    def test_cooldown_expiry_allows_reassessment(self):
        self.assertFalse(self.check(3, 15))

    def test_old_loss_streak_does_not_permanently_block(self):
        self.assertFalse(self.check(3, 60*24*30))

    def test_new_loss_restarts_cooldown(self):
        self.assertTrue(self.check(4, 0))

    def test_below_limit_does_not_block(self):
        self.assertFalse(self.check(2, 0))
