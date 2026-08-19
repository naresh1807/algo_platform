"""Shared authentication and reliability helpers for WebSocket traffic."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from rest_framework.authtoken.models import Token

from common.authentication import token_is_expired
from common.permissions import ADMIN_GROUP_NAME, TRADER_GROUP_NAME

logger = logging.getLogger(__name__)

TOKEN_SUBPROTOCOL = "drf-token"
UNAUTHORIZED_CLOSE_CODE = 4401
SERVER_ERROR_CLOSE_CODE = 1011
_DRF_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _token_from_subprotocols(subprotocols: Sequence[str] | None) -> tuple[str | None, bool]:
    """Return the credential following ``drf-token`` and whether it was offered."""
    protocols = list(subprotocols or ())
    try:
        marker_index = protocols.index(TOKEN_SUBPROTOCOL)
    except ValueError:
        return None, False

    if marker_index + 1 >= len(protocols):
        return None, True
    credential = protocols[marker_index + 1].strip()
    return credential or None, True


def _token_from_headers(headers: Sequence[tuple[bytes, bytes]] | None) -> tuple[str | None, bool]:
    """Support native clients that can send ``Authorization: Token <key>``."""
    for raw_name, raw_value in headers or ():
        if raw_name.lower() != b"authorization":
            continue
        try:
            scheme, credential = raw_value.decode("ascii").strip().split(None, 1)
        except (UnicodeDecodeError, ValueError):
            return None, True
        if scheme.lower() != "token":
            return None, True
        credential = credential.strip()
        return credential or None, True
    return None, False


def extract_token(scope: Mapping) -> tuple[str | None, bool, str | None]:
    """Extract a DRF token without ever placing it in the URL/query string.

    Browsers cannot set an Authorization header on a WebSocket handshake, so
    they offer ``["drf-token", token]`` as subprotocols. Native clients may
    instead use the regular DRF ``Authorization: Token ...`` header.
    """
    credential, presented = _token_from_subprotocols(scope.get("subprotocols"))
    if presented:
        return credential, True, TOKEN_SUBPROTOCOL

    credential, presented = _token_from_headers(scope.get("headers"))
    return credential, presented, None


def _has_dashboard_role(user) -> bool:
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=(TRADER_GROUP_NAME, ADMIN_GROUP_NAME)).exists()


@database_sync_to_async
def _user_for_token(credential: str):
    try:
        token = Token.objects.select_related("user").get(key=credential)
    except Token.DoesNotExist:
        return AnonymousUser()
    if token_is_expired(token):
        token.delete()
        return AnonymousUser()
    if not _has_dashboard_role(token.user):
        return AnonymousUser()
    return token.user


@database_sync_to_async
def _user_id_is_authorized(user_id: int | None) -> bool:
    if not user_id:
        return False
    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return False
    return _has_dashboard_role(user)


class TokenAuthMiddleware:
    """Overlay DRF token authentication on Channels session authentication.

    This middleware belongs *inside* ``AuthMiddlewareStack``. With no token it
    preserves the session-authenticated user. An explicitly supplied invalid
    token fails closed instead of silently falling back to a session.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        credential, presented, selected_protocol = extract_token(scope)
        if presented:
            scope["user"] = AnonymousUser()
            if credential and _DRF_TOKEN_PATTERN.fullmatch(credential):
                try:
                    scope["user"] = await _user_for_token(credential)
                    if scope["user"].is_authenticated:
                        # Retained only in process memory so every outbound
                        # event can prove this exact token still exists. This
                        # makes login-time rotation and explicit revocation
                        # take effect on an already-open socket.
                        scope["auth_token_key"] = credential
                except Exception:
                    # Authentication infrastructure failures fail closed. Do
                    # not log the credential or any handshake header values.
                    logger.exception("WebSocket token authentication failed")
            if selected_protocol is not None:
                scope["auth_subprotocol"] = selected_protocol
        return await self.app(scope, receive, send)


class AuthenticatedWebsocketConsumer(AsyncWebsocketConsumer):
    """Consumer base enforcing authentication, token TTL, and dashboard RBAC."""

    group_name: str | None = None

    async def _authorization_is_current(self) -> bool:
        try:
            credential = self.scope.get("auth_token_key")
            if credential:
                user = await _user_for_token(credential)
                expected_user_id = getattr(self.scope.get("user"), "pk", None)
                return user.is_authenticated and user.pk == expected_user_id
            return await _user_id_is_authorized(
                getattr(self.scope.get("user"), "pk", None)
            )
        except Exception:
            # Database/auth infrastructure failures must fail closed and
            # must never put a credential into logs.
            logger.exception("WebSocket authorization revalidation failed")
            return False

    async def join_group_and_accept(self, group_name: str) -> bool:
        if not await self._authorization_is_current():
            await self.close(code=UNAUTHORIZED_CLOSE_CODE)
            return False

        try:
            await self.channel_layer.group_add(group_name, self.channel_name)
        except Exception:
            logger.exception("Unable to join WebSocket channel group %s", group_name)
            await self.close(code=SERVER_ERROR_CLOSE_CODE)
            return False

        self.group_name = group_name
        await self.accept(subprotocol=self.scope.get("auth_subprotocol"))
        return True

    async def send(self, text_data=None, bytes_data=None, close=False):
        """Revalidate before every server push, including long-lived sockets."""
        if not await self._authorization_is_current():
            await self.close(code=UNAUTHORIZED_CLOSE_CODE)
            return
        await super().send(text_data=text_data, bytes_data=bytes_data, close=close)

    async def disconnect(self, code):
        if self.group_name is None:
            return
        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception:
            # A dead Redis connection during cleanup should not turn a normal
            # client disconnect into an application error.
            logger.warning(
                "Unable to leave WebSocket channel group %s",
                self.group_name,
                exc_info=True,
            )


def broadcast_group(group_name: str, event: dict, *, log: logging.Logger | None = None) -> bool:
    """Best-effort synchronous group broadcast for model signal handlers.

    Persistence is the source of truth; a Redis/channel-layer outage must not
    make a successful model save appear to have failed. The return value is
    useful to direct callers/tests but signal receivers intentionally ignore it.
    """
    from asgiref.sync import async_to_sync

    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return False
        async_to_sync(channel_layer.group_send)(group_name, event)
    except Exception:
        (log or logger).exception(
            "Failed to broadcast to WebSocket group %s; persisted data is unchanged",
            group_name,
        )
        return False
    return True
