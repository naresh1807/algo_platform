from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from .indicators import MIN_CANDLES_REQUIRED, compute_indicators
from .models import HistoricalData


def _make_candle(symbol, timeframe, minutes_ago, close):
    return HistoricalData.objects.create(
        symbol=symbol, timeframe=timeframe,
        timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        open=Decimal(str(close)), high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)), close=Decimal(str(close)),
        volume=10000, source="test",
    )


class HistoricalDataModelTests(TestCase):
    def test_unique_together_prevents_duplicate_candle(self):
        """
        The whole point of unique_together=(symbol, timeframe, timestamp)
        is that re-ingesting the same candle twice must not create a
        duplicate row -- this is what apps.market_data.tasks relies on
        for its bulk_create(ignore_conflicts=True) upsert pattern.
        """
        ts = timezone.now()
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=ts,
            open=100, high=101, low=99, close=100, volume=1000, source="test",
        )
        with self.assertRaises(Exception):
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="5m", timestamp=ts,
                open=200, high=201, low=199, close=200, volume=2000, source="test",
            )


class ComputeIndicatorsTests(TestCase):
    def test_returns_none_with_insufficient_candles(self):
        """
        apps.signals.engine relies on this returning None (not raising)
        when there isn't enough history yet, treating it as a normal
        NO_TRADE state rather than an error.
        """
        for i in range(MIN_CANDLES_REQUIRED - 10):
            _make_candle("NIFTY", "5m", i, 24500 + i)
        self.assertIsNone(compute_indicators("NIFTY", "5m"))

    def test_returns_indicator_dict_with_enough_candles(self):
        for i in range(MIN_CANDLES_REQUIRED + 10, 0, -1):
            _make_candle("NIFTY", "5m", i, 24500 + (i % 5))
        result = compute_indicators("NIFTY", "5m")
        self.assertIsNotNone(result)
        for key in ("close", "ema9", "ema21", "rsi", "atr", "adx", "bb_width", "relative_volume"):
            self.assertIn(key, result)


class SmartApiCircuitBreakerTests(TestCase):
    """
    apps.market_data.broker_client's extended-cooldown circuit breaker
    -- module-level process state (NOT DB-backed), so unlike every
    other test in this file, TestCase's transaction rollback does
    nothing for it; setUp/addCleanup explicitly reset it instead.
    Real AB1021 behavior (see this project's own incident history):
    isolated, widely-spaced calls kept failing even with the existing
    per-call retry/backoff, meaning it's an account-level cooldown --
    every retry during that window is another failed request, plausibly
    extending it. This breaker stops attempting calls once several in a
    row have each exhausted their own retries and are still rate-limited.
    """

    def setUp(self):
        from apps.market_data import broker_client

        self._broker_client_module = broker_client
        self._reset_module_state()
        self.addCleanup(self._reset_module_state)

        # Don't actually sleep through backoff/pacer delays in tests --
        # same pattern apps.execution.tests.WaitForFillTests already
        # uses for live_executor.time.sleep.
        original_sleep = broker_client.time.sleep
        broker_client.time.sleep = lambda _: None
        self.addCleanup(setattr, broker_client.time, "sleep", original_sleep)

    def _reset_module_state(self):
        m = self._broker_client_module
        m._smartapi_consecutive_rate_limit_exhaustions = 0
        m._smartapi_cooldown_until = 0.0
        m._SMARTAPI_LAST_REQUEST_AT = 0.0

    def test_trips_after_consecutive_exhaustions_and_skips_further_calls(self):
        from apps.market_data.broker_client import BrokerClient, SmartApiCooldownActive

        client = BrokerClient()
        always_rate_limited = lambda: {"message": "Too many requests", "errorcode": "AB1021"}

        # Two calls in a row, each exhausting its own retries -- trips the breaker.
        for _ in range(2):
            response = client._smartapi_request(always_rate_limited)
            self.assertEqual(response.get("errorcode"), "AB1021")

        calls_made = []

        def should_never_be_called():
            calls_made.append(1)
            return {"ok": True}

        with self.assertRaises(SmartApiCooldownActive):
            client._smartapi_request(should_never_be_called)
        self.assertEqual(calls_made, [], "circuit breaker must skip the call entirely, not just fail fast after calling it")

    def test_success_resets_the_consecutive_counter(self):
        from apps.market_data.broker_client import BrokerClient

        client = BrokerClient()
        always_rate_limited = lambda: {"message": "Too many requests", "errorcode": "AB1021"}
        succeeds = lambda: {"ok": True}

        client._smartapi_request(always_rate_limited)  # 1 exhaustion
        client._smartapi_request(succeeds)  # resets the streak
        client._smartapi_request(always_rate_limited)  # back to 1 -- must NOT trip

        self.assertEqual(self._broker_client_module._smartapi_consecutive_rate_limit_exhaustions, 1)
        self.assertEqual(self._broker_client_module._smartapi_cooldown_until, 0.0)
