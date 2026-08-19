import asyncio
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from common.websockets import TOKEN_SUBPROTOCOL, extract_token


IN_MEMORY_CHANNEL_LAYER = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}
ALL_WEBSOCKET_PATHS = (
    "/ws/market-data/live/",
    "/ws/signals/live/",
    "/ws/risk/live/",
    "/ws/options/live/",
    "/ws/options/candles/1/5m/",
    "/ws/jarvis/live/",
    "/ws/investing/index-live/",
)
LOCAL_ORIGIN_HEADERS = [(b"origin", b"http://localhost:3000")]


class TokenExtractionTests(SimpleTestCase):
    def test_browser_subprotocol_does_not_require_query_string(self):
        credential, presented, selected = extract_token(
            {"subprotocols": [TOKEN_SUBPROTOCOL, "a" * 40], "headers": []}
        )

        self.assertEqual(credential, "a" * 40)
        self.assertTrue(presented)
        self.assertEqual(selected, TOKEN_SUBPROTOCOL)

    def test_native_authorization_header_is_supported(self):
        credential, presented, selected = extract_token(
            {"subprotocols": [], "headers": [(b"authorization", b"Token " + b"b" * 40)]}
        )

        self.assertEqual(credential, "b" * 40)
        self.assertTrue(presented)
        self.assertIsNone(selected)

    def test_malformed_explicit_credentials_fail_closed(self):
        self.assertEqual(
            extract_token({"subprotocols": [TOKEN_SUBPROTOCOL], "headers": []}),
            (None, True, TOKEN_SUBPROTOCOL),
        )
        self.assertEqual(
            extract_token(
                {"subprotocols": [], "headers": [(b"authorization", b"Bearer secret")]}
            ),
            (None, True, None),
        )


@override_settings(
    CHANNEL_LAYERS=IN_MEMORY_CHANNEL_LAYER,
    ALLOWED_HOSTS=["localhost", "testserver"],
)
class WebsocketAuthenticationTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="websocket-user",
            password="not-used-by-websocket-tests",
        )
        trader_group, _ = Group.objects.get_or_create(name="Trader")
        self.user.groups.add(trader_group)
        self.token = Token.objects.create(user=self.user)

    @staticmethod
    def _application():
        from config.asgi import application

        return application

    def test_valid_drf_token_connects_to_every_route(self):
        async def scenario():
            for path in ALL_WEBSOCKET_PATHS:
                communicator = WebsocketCommunicator(
                    self._application(),
                    path,
                    headers=LOCAL_ORIGIN_HEADERS,
                    subprotocols=[TOKEN_SUBPROTOCOL, self.token.key],
                )
                connected, selected = await communicator.connect()
                try:
                    self.assertTrue(connected, path)
                    self.assertEqual(selected, TOKEN_SUBPROTOCOL, path)
                finally:
                    if connected:
                        await communicator.disconnect()

        asyncio.run(scenario())

    def test_anonymous_and_invalid_tokens_are_rejected_on_every_route(self):
        async def scenario():
            for protocols in ([], [TOKEN_SUBPROTOCOL, "0" * 40]):
                for path in ALL_WEBSOCKET_PATHS:
                    communicator = WebsocketCommunicator(
                        self._application(),
                        path,
                        headers=LOCAL_ORIGIN_HEADERS,
                        subprotocols=protocols,
                    )
                    connected, close_code = await communicator.connect()
                    self.assertFalse(connected, path)
                    self.assertEqual(close_code, 4401, path)

        asyncio.run(scenario())

    def test_disallowed_origin_is_rejected_before_authentication(self):
        async def scenario():
            communicator = WebsocketCommunicator(
                self._application(),
                ALL_WEBSOCKET_PATHS[0],
                headers=[(b"origin", b"https://attacker.example")],
                subprotocols=[TOKEN_SUBPROTOCOL, self.token.key],
            )
            connected, _ = await communicator.connect()
            self.assertFalse(connected)

        asyncio.run(scenario())

    def test_native_authorization_header_authenticates_without_a_subprotocol(self):
        async def scenario():
            communicator = WebsocketCommunicator(
                self._application(),
                ALL_WEBSOCKET_PATHS[0],
                headers=LOCAL_ORIGIN_HEADERS
                + [(b"authorization", f"Token {self.token.key}".encode("ascii"))],
            )
            connected, selected = await communicator.connect()
            try:
                self.assertTrue(connected)
                self.assertIsNone(selected)
            finally:
                if connected:
                    await communicator.disconnect()

        asyncio.run(scenario())

    def test_authenticated_user_without_trader_or_admin_role_is_rejected(self):
        unprivileged = get_user_model().objects.create_user(username="no-dashboard-role")
        token = Token.objects.create(user=unprivileged)

        async def scenario():
            communicator = WebsocketCommunicator(
                self._application(),
                ALL_WEBSOCKET_PATHS[0],
                headers=LOCAL_ORIGIN_HEADERS,
                subprotocols=[TOKEN_SUBPROTOCOL, token.key],
            )
            connected, close_code = await communicator.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4401)

        asyncio.run(scenario())

    def test_inactive_user_is_rejected(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        async def scenario():
            communicator = WebsocketCommunicator(
                self._application(),
                ALL_WEBSOCKET_PATHS[0],
                headers=LOCAL_ORIGIN_HEADERS,
                subprotocols=[TOKEN_SUBPROTOCOL, self.token.key],
            )
            connected, close_code = await communicator.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4401)

        asyncio.run(scenario())

    @override_settings(AUTH_TOKEN_TTL_HOURS=1)
    def test_expired_token_is_rejected_during_handshake(self):
        Token.objects.filter(pk=self.token.pk).update(
            created=timezone.now() - timedelta(hours=2)
        )

        async def scenario():
            communicator = WebsocketCommunicator(
                self._application(),
                ALL_WEBSOCKET_PATHS[0],
                headers=LOCAL_ORIGIN_HEADERS,
                subprotocols=[TOKEN_SUBPROTOCOL, self.token.key],
            )
            connected, close_code = await communicator.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4401)

        asyncio.run(scenario())

    def _assert_connection_closes_after(self, mutation):
        async def scenario():
            communicator = WebsocketCommunicator(
                self._application(),
                ALL_WEBSOCKET_PATHS[0],
                headers=LOCAL_ORIGIN_HEADERS,
                subprotocols=[TOKEN_SUBPROTOCOL, self.token.key],
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            await database_sync_to_async(mutation)()
            await get_channel_layer().group_send(
                "market_data_live",
                {"type": "candle_update", "data": {"symbol": "TEST"}},
            )
            output = await communicator.receive_output(timeout=1)
            self.assertEqual(output["type"], "websocket.close")
            self.assertEqual(output["code"], 4401)

        asyncio.run(scenario())

    def test_deleted_or_rotated_token_stops_an_open_socket(self):
        token_key = self.token.key

        def rotate_token():
            Token.objects.filter(key=token_key).delete()
            Token.objects.create(user_id=self.user.pk)

        self._assert_connection_closes_after(rotate_token)

    @override_settings(AUTH_TOKEN_TTL_HOURS=1)
    def test_token_that_expires_stops_an_open_socket(self):
        token_key = self.token.key

        def expire_token():
            Token.objects.filter(key=token_key).update(
                created=timezone.now() - timedelta(hours=2)
            )

        self._assert_connection_closes_after(expire_token)


class _FailingChannelLayer:
    async def group_send(self, group_name, event):
        raise ConnectionError("simulated Redis outage")


class BroadcastFailureIsolationTests(TransactionTestCase):
    """A channel-layer outage must never roll back the source model save."""

    def _outage(self):
        return patch("common.websockets.get_channel_layer", return_value=_FailingChannelLayer())

    def test_market_candle_save_survives_channel_layer_failure(self):
        from apps.market_data.models import HistoricalData

        with self._outage(), self.assertLogs("apps.market_data.signals", level="ERROR"):
            row = HistoricalData.objects.create(
                symbol="NIFTY",
                timeframe="5m",
                timestamp=timezone.now(),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=10,
                source="test",
            )

        self.assertTrue(HistoricalData.objects.filter(pk=row.pk).exists())

    def test_option_snapshot_save_survives_channel_layer_failure(self):
        from apps.options.models import OptionChainSnapshot, OptionContract

        contract = OptionContract.objects.create(
            underlying="NIFTY",
            expiry=date(2099, 1, 1),
            strike=Decimal("25000"),
            option_type=OptionContract.OptionType.CALL,
            symbol_token="ws-resilience-test",
        )
        with self._outage(), self.assertLogs("apps.options.signals", level="ERROR"):
            row = OptionChainSnapshot.objects.create(
                contract=contract,
                timestamp=timezone.now(),
                ltp=Decimal("100"),
                open_interest=1000,
                change_in_oi=10,
                volume=100,
            )

        self.assertTrue(OptionChainSnapshot.objects.filter(pk=row.pk).exists())

    def test_investing_snapshot_save_survives_channel_layer_failure(self):
        from apps.investing.models import Index, IndexPriceSnapshot

        index = Index.objects.create(name="WS TEST INDEX", symbol="WSTEST")
        with self._outage(), self.assertLogs("apps.investing.signals", level="ERROR"):
            row = IndexPriceSnapshot.objects.create(
                index=index,
                timestamp=timezone.now(),
                ltp=Decimal("20000"),
            )

        self.assertTrue(IndexPriceSnapshot.objects.filter(pk=row.pk).exists())

    def test_feed_health_save_survives_channel_layer_failure(self):
        from apps.monitoring.models import FeedHealthCheck

        with self._outage(), self.assertLogs("apps.monitoring.signals", level="ERROR"):
            row = FeedHealthCheck.objects.create(source="test", is_healthy=False)

        self.assertTrue(FeedHealthCheck.objects.filter(pk=row.pk).exists())
