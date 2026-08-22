"""
API-level regression coverage for apps.paper_trading.views -- distinct
from the other tests_*.py files here, which all exercise the service
layer directly. Covers a real bug found during a performance/bug audit:
PaperOrderViewSet and PaperAIDecisionViewSet combined filterset_fields
(this project's DEFAULT_FILTER_BACKENDS is DjangoFilterBackend) with a
hard [:200]-sliced queryset -- Django raises "Cannot filter a query
once a slice has been taken" the moment a matching query param (e.g.
?status=filled) is supplied, a 500 the frontend would have hit the
first time it filtered either endpoint.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Action, PaperAIDecision, get_or_create_account


def _trader_client() -> APIClient:
    user = get_user_model().objects.create_user(username="paper_trader_api", password="pw")
    user.groups.add(Group.objects.get_or_create(name="Trader")[0])
    client = APIClient()
    client.force_authenticate(user)
    return client


class PaperOrderAndDecisionFilterTests(TestCase):
    def setUp(self):
        self.client = _trader_client()
        self.account = get_or_create_account()

    def test_filtering_orders_by_status_does_not_500(self):
        response = self.client.get("/api/paper-trading/orders/", {"status": "filled"})
        self.assertEqual(response.status_code, 200)

    def test_filtering_orders_by_purpose_does_not_500(self):
        response = self.client.get("/api/paper-trading/orders/", {"purpose": "entry"})
        self.assertEqual(response.status_code, 200)

    def test_filtering_decisions_by_action_does_not_500(self):
        PaperAIDecision.objects.create(
            decision_id="11111111-1111-1111-1111-111111111111", timestamp=timezone.now(), symbol="NIFTY",
            feature_snapshot_json={}, feature_schema_version="v1",
            action=Action.HOLD, is_hypothetical=True, explanation_json={},
        )
        response = self.client.get("/api/paper-trading/decisions/", {"action": "hold"})
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data["results"]), 1)

    def test_filtering_decisions_by_is_hypothetical_does_not_500(self):
        response = self.client.get("/api/paper-trading/decisions/", {"is_hypothetical": "true"})
        self.assertEqual(response.status_code, 200)

    def test_unfiltered_list_endpoints_still_work(self):
        for path in ("/api/paper-trading/orders/", "/api/paper-trading/decisions/", "/api/paper-trading/positions/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
