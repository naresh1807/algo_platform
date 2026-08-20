from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase


class TokenAuthTests(APITestCase):
    """
    This is the exact endpoint whose absence from config/urls.py caused
    every dashboard API call to 403 earlier in this project's history
    (Login.jsx had nothing to actually call) -- a test here means that
    specific regression can't silently happen again.
    """

    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.user = User.objects.create_user(username="trader1", password="a-real-password-123")
        self.user.groups.add(Group.objects.get_or_create(name="Trader")[0])

    def test_valid_credentials_return_a_token(self):
        response = self.client.post(
            "/api/auth/token/", {"username": "trader1", "password": "a-real-password-123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.data)

    def test_invalid_credentials_rejected(self):
        response = self.client.post(
            "/api/auth/token/", {"username": "trader1", "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 400)

    def test_successful_login_rotates_the_previous_token(self):
        first = self.client.post(
            "/api/auth/token/", {"username": "trader1", "password": "a-real-password-123"},
        ).data["token"]
        second = self.client.post(
            "/api/auth/token/", {"username": "trader1", "password": "a-real-password-123"},
        ).data["token"]

        self.assertNotEqual(first, second)
        self.assertEqual(Token.objects.filter(user=self.user).count(), 1)
        self.assertEqual(
            self.client.get("/api/risk/equity/", HTTP_AUTHORIZATION=f"Token {first}").status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/api/risk/equity/", HTTP_AUTHORIZATION=f"Token {second}").status_code,
            200,
        )

    def test_authenticated_request_cannot_bypass_login_rate_limit(self):
        """
        DRF's SimpleRateThrottle subclasses (LoginRateThrottle here) read
        their rate from a THROTTLE_RATES CLASS ATTRIBUTE that is bound
        once, at import time, from api_settings.DEFAULT_THROTTLE_RATES --
        `@override_settings(REST_FRAMEWORK=...)` fires DRF's own
        setting_changed reload for api_settings itself, but does NOT
        retroactively update a throttle class's already-bound
        THROTTLE_RATES attribute (a well-known DRF testing gotcha), so
        the previous version of this test silently never actually
        throttled anything -- every attempt returned 400 (wrong
        password), never 429. Patching LoginRateThrottle.THROTTLE_RATES
        directly is what actually takes effect per-request, since DRF
        instantiates a fresh throttle object on every request and reads
        this attribute then.
        """
        from unittest.mock import patch

        from .views import LoginRateThrottle

        self.client.force_authenticate(self.user)
        payload = {"username": "trader1", "password": "wrong-password"}
        with patch.object(LoginRateThrottle, "THROTTLE_RATES", {"login": "2/minute"}):
            self.assertEqual(self.client.post("/api/auth/token/", payload).status_code, 400)
            self.assertEqual(self.client.post("/api/auth/token/", payload).status_code, 400)
            self.assertEqual(self.client.post("/api/auth/token/", payload).status_code, 429)

    def test_protected_endpoint_rejects_missing_token(self):
        response = self.client.get("/api/risk/equity/")
        self.assertEqual(response.status_code, 401)

    def test_protected_endpoint_accepts_valid_token(self):
        token_response = self.client.post(
            "/api/auth/token/", {"username": "trader1", "password": "a-real-password-123"},
        )
        token = token_response.data["token"]
        response = self.client.get("/api/risk/equity/", HTTP_AUTHORIZATION=f"Token {token}")
        self.assertEqual(response.status_code, 200)

    def test_logout_revokes_the_presented_token(self):
        token = Token.objects.create(user=self.user)
        authorization = f"Token {token.key}"

        response = self.client.post("/api/auth/logout/", HTTP_AUTHORIZATION=authorization)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Token.objects.filter(pk=token.pk).exists())
        self.assertEqual(
            self.client.get("/api/risk/equity/", HTTP_AUTHORIZATION=authorization).status_code,
            401,
        )

    def test_logout_requires_a_valid_presented_token(self):
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, 401)
