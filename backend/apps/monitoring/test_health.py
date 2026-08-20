"""
apps.monitoring.health.SystemHealthView -- fix-list item 8. Verifies the
health endpoint correctly reports stale/missing services rather than a
false "healthy" (the exact gap that let the priority-worker/live-feed
starvation incidents go unnoticed before).
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.market_data import feed_stats
from common.permissions import TRADER_GROUP_NAME


class SystemHealthViewTests(APITestCase):
    def setUp(self):
        cache.clear()
        user = get_user_model().objects.create_user(username="trader1", password="x")
        Group.objects.get_or_create(name=TRADER_GROUP_NAME)[0].user_set.add(user)
        self.client.force_authenticate(user=user)
        self.url = reverse("monitoring:system-health")

    def tearDown(self):
        cache.clear()

    def test_missing_heartbeat_is_reported_stale(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["live_feed"]["process_heartbeat"]["stale"])
        self.assertIsNone(res.data["live_feed"]["process_heartbeat"]["at"])

    def test_fresh_heartbeat_is_reported_healthy(self):
        feed_stats.mark_heartbeat()
        res = self.client.get(self.url)
        self.assertFalse(res.data["live_feed"]["process_heartbeat"]["stale"])

    @override_settings(LIVE_FEED_STALE_SECONDS=5)
    def test_old_heartbeat_beyond_threshold_is_stale(self):
        old = (timezone.now() - timedelta(seconds=30)).isoformat()
        cache.set("livefeed:heartbeat_at", old, timeout=300)
        res = self.client.get(self.url)
        self.assertTrue(res.data["live_feed"]["process_heartbeat"]["stale"])

    @override_settings(CELERY_HEARTBEAT_STALE_SECONDS=30)
    def test_priority_worker_heartbeat_missing_is_reported(self):
        cache.set("celery:heartbeat:celery", timezone.now().isoformat(), timeout=300)
        # Deliberately no "celery:heartbeat:priority" key -- simulates the
        # exact fix-list item 7 incident (priority worker never started).
        res = self.client.get(self.url)
        self.assertFalse(res.data["celery_default_worker"]["stale"])
        self.assertTrue(res.data["celery_priority_worker"]["stale"])

    def test_connection_state_and_subscribed_token_count_are_surfaced(self):
        feed_stats.set_connection_state("connected")
        feed_stats.set_subscribed_token_count(42)
        res = self.client.get(self.url)
        self.assertEqual(res.data["live_feed"]["connection_state"], "connected")
        self.assertEqual(res.data["live_feed"]["subscribed_option_token_count"], 42)

    def test_last_error_category_is_surfaced(self):
        feed_stats.record_error("rate_limit", "Access denied because of exceeding access rate")
        res = self.client.get(self.url)
        self.assertEqual(res.data["live_feed"]["last_error"]["category"], "rate_limit")

    def test_health_response_never_includes_credential_fields(self):
        res = self.client.get(self.url)
        body_str = str(res.data)
        for forbidden in ("ANGEL_ONE_PASSWORD", "ANGEL_ONE_TOTP_SECRET", "SECRET_KEY", "jwt_token", "feed_token"):
            self.assertNotIn(forbidden, body_str)

    def test_unauthenticated_request_is_rejected(self):
        self.client.force_authenticate(user=None)
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 401)
