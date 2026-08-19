from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from apps.admin_tools.models import AuditLog
from apps.options.models import OptionsStrategySetting


class DashboardRoleBoundaryTests(APITestCase):
    """Authenticated accounts without an assigned platform role stay outside the dashboard."""

    READ_ENDPOINTS = (
        "/api/signals/",
        "/api/market-data/candles/",
        "/api/news/sentiment/",
        "/api/options/contracts/",
        "/api/learning/model-registry/",
        "/api/monitoring/feed-health/",
        "/api/analytics/performance/",
        "/api/investing/stocks/",
        "/api/jarvis/suggested/",
    )

    def setUp(self):
        User = get_user_model()
        trader_group = Group.objects.create(name="Trader")
        admin_group = Group.objects.create(name="Admin")

        self.roleless = User.objects.create_user(username="roleless", password="pw")
        self.trader = User.objects.create_user(username="trader", password="pw")
        self.trader.groups.add(trader_group)
        self.admin = User.objects.create_user(username="admin", password="pw")
        self.admin.groups.add(admin_group)

    @staticmethod
    def _client_for(user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_roleless_authenticated_user_cannot_read_dashboard_apis(self):
        client = self._client_for(self.roleless)
        for endpoint in self.READ_ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(client.get(endpoint).status_code, 403)

    def test_trader_can_read_dashboard_apis(self):
        client = self._client_for(self.trader)
        for endpoint in self.READ_ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(client.get(endpoint).status_code, 200)

    def test_roleless_user_cannot_use_personal_write_endpoints(self):
        client = self._client_for(self.roleless)
        attempts = (
            ("/api/investing/watchlist/", {"stock_symbol": "INFY"}),
            (
                "/api/monitoring/price-alerts/",
                {"symbol": "NIFTY", "condition": "above", "target_price": "25000"},
            ),
            ("/api/jarvis/command/", {"text": "open portfolio"}),
        )
        for endpoint, payload in attempts:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(client.post(endpoint, payload, format="json").status_code, 403)


class ConsequentialWritePermissionTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        trader_group = Group.objects.create(name="Trader")
        admin_group = Group.objects.create(name="Admin")
        self.trader = User.objects.create_user(username="trader", password="pw")
        self.trader.groups.add(trader_group)
        self.admin = User.objects.create_user(username="admin", password="pw")
        self.admin.groups.add(admin_group)
        self.url = reverse("options:options-strategy-settings")

    @staticmethod
    def _client_for(user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_strategy_settings_get_is_read_only_for_trader(self):
        response = self._client_for(self.trader).get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(OptionsStrategySetting.objects.exists())

    def test_trader_cannot_change_platform_strategy_settings(self):
        response = self._client_for(self.trader).post(
            self.url, {"strike_mode": "atm"}, format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(OptionsStrategySetting.objects.exists())

    def test_admin_change_is_persisted_and_audited(self):
        response = self._client_for(self.admin).post(
            self.url, {"strike_mode": "atm"}, format="json",
        )

    def test_admin_cannot_select_an_unsynchronized_custom_expiry(self):
        response = self._client_for(self.admin).post(
            self.url,
            {"expiry_mode": "custom", "custom_expiry": "2099-01-01"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("custom_expiry", response.data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(OptionsStrategySetting.objects.get(pk=1).strike_mode, "atm")
        self.assertTrue(
            AuditLog.objects.filter(
                action="options_strategy_settings_changed", actor=self.admin,
            ).exists(),
        )

    @patch("apps.options.index_direction_strategy.evaluate_index_direction_trade")
    def test_manual_signal_evaluation_requires_admin(self, evaluate):
        response = self._client_for(self.trader).post(
            reverse("options:options-evaluate-now"),
            {"underlying": "NIFTY", "timeframe": "5m"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        evaluate.assert_not_called()

    @patch("apps.admin_tools.tasks.run_database_backup.delay")
    def test_jarvis_backup_command_requires_admin(self, backup_delay):
        trader_response = self._client_for(self.trader).post(
            reverse("jarvis:jarvis-command"), {"text": "backup database"}, format="json",
        )
        self.assertEqual(trader_response.status_code, 200)
        self.assertFalse(trader_response.data["success"])
        self.assertIn("Admin", trader_response.data["text"])
        backup_delay.assert_not_called()

        admin_response = self._client_for(self.admin).post(
            reverse("jarvis:jarvis-command"), {"text": "backup database"}, format="json",
        )
        self.assertEqual(admin_response.status_code, 200)
        self.assertTrue(admin_response.data["success"])
        backup_delay.assert_called_once_with()
