"""Authentication controls shared by REST and operational endpoints."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed


def token_is_expired(token, *, now=None) -> bool:
    """Return whether a DRF token is outside the shared configured TTL."""
    ttl_hours = int(getattr(settings, "AUTH_TOKEN_TTL_HOURS", 12))
    return token.created <= (now or timezone.now()) - timedelta(hours=ttl_hours)


class ExpiringTokenAuthentication(TokenAuthentication):
    """DRF token authentication with a bounded server-configurable lifetime."""

    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        if token_is_expired(token):
            token.delete()
            raise AuthenticationFailed("Authentication token has expired.")
        return user, token
