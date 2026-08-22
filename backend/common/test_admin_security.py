"""
common.admin_security.AdminLoginRateLimitMiddleware -- Django's built-in
admin login has no brute-force protection of its own (DRF's throttle
never sees it, see that module's docstring for why this matters more
than the API login: a brute-forced superuser bypasses every RBAC check).
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase, override_settings


@override_settings(
    REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": {"login": "3/minute"}},
)
class AdminLoginRateLimitMiddlewareTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_attempts_within_the_limit_reach_the_admin_view(self):
        for _ in range(3):
            response = self.client.post("/admin/login/", {"username": "x", "password": "wrong"})
            self.assertNotEqual(response.status_code, 429)

    def test_exceeding_the_limit_is_blocked_with_429(self):
        for _ in range(3):
            self.client.post("/admin/login/", {"username": "x", "password": "wrong"})
        response = self.client.post("/admin/login/", {"username": "x", "password": "wrong"})
        self.assertEqual(response.status_code, 429)

    def test_throttle_is_scoped_per_client_ip(self):
        for _ in range(3):
            self.client.post(
                "/admin/login/", {"username": "x", "password": "wrong"},
                REMOTE_ADDR="10.0.0.1",
            )
        blocked = self.client.post(
            "/admin/login/", {"username": "x", "password": "wrong"}, REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(blocked.status_code, 429)

        still_allowed = self.client.post(
            "/admin/login/", {"username": "x", "password": "wrong"}, REMOTE_ADDR="10.0.0.2",
        )
        self.assertNotEqual(still_allowed.status_code, 429)

    def test_other_admin_paths_are_not_throttled(self):
        responses = [self.client.get("/admin/") for _ in range(5)]
        self.assertTrue(all(r.status_code != 429 for r in responses))

    def test_get_requests_to_login_are_not_throttled(self):
        responses = [self.client.get("/admin/login/") for _ in range(5)]
        self.assertTrue(all(r.status_code != 429 for r in responses))
