from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase


class TokenAuthTests(APITestCase):
    """
    This is the exact endpoint whose absence from config/urls.py caused
    every dashboard API call to 403 earlier in this project's history
    (Login.jsx had nothing to actually call) -- a test here means that
    specific regression can't silently happen again.
    """

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="trader1", password="a-real-password-123")

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
