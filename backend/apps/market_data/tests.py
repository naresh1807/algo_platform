from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from .indicators import MIN_CANDLES_REQUIRED, compute_indicators
from .models import HistoricalData


class WatchlistIngestionScheduleTests(TestCase):
    def test_five_minute_schedule_excludes_one_minute_timeframe(self):
        from config.celery import app

        entry = app.conf.beat_schedule["ingest-watchlist-candles-every-5-minutes"]
        self.assertEqual(entry["kwargs"], {"exclude_timeframes": ["1m"]})

    @override_settings(
        BROKER_MODE="live",
        WATCHLIST=("NIFTY",),
        CHART_TIMEFRAMES=("1m", "3m", "5m"),
    )
    def test_excluding_one_minute_preserves_all_other_timeframes(self):
        from .tasks import ingest_watchlist_candles

        with (
            patch("apps.market_data.tasks.get_broker_client") as get_client,
            patch("apps.market_data.tasks._upsert_candles", return_value=0),
            patch("apps.market_data.tasks._record_feed_health"),
        ):
            get_client.return_value.fetch_recent_candles.return_value = []
            ingest_watchlist_candles(exclude_timeframes=["1m"])

        requested_timeframes = [
            call.args[1]
            for call in get_client.return_value.fetch_recent_candles.call_args_list
        ]
        self.assertEqual(requested_timeframes, ["3m", "5m"])


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


class SmartApiHardTimeoutTests(TestCase):
    """
    apps.market_data.broker_client._smartapi_request's hard per-call
    timeout, and _wait_for_smartapi_slot's bounded wait for the shared
    cross-process rate-limit slot -- both fix REAL, OBSERVED incidents:
    apps.options.broker_client.OptionChainClient.fetch_chain_quotes
    (routed through _smartapi_request, see that module's own comment)
    hung for 160+ seconds with the process at 0% CPU, and separately,
    _wait_for_smartapi_slot's distributed-lock wait loop had NO bound at
    all and could starve indefinitely under multi-process contention.
    On Celery's --pool=solo (one task at a time), either hang silently
    blocked every other task queued behind it on the same worker --
    including the heartbeat task the frontend's "Priority Worker
    Missing" health status depends on.
    """

    def setUp(self):
        from apps.market_data import broker_client

        self._broker_client_module = broker_client
        self._reset_module_state()
        self.addCleanup(self._reset_module_state)

        original_sleep = broker_client.time.sleep
        broker_client.time.sleep = lambda _: None
        self.addCleanup(setattr, broker_client.time, "sleep", original_sleep)

    def _reset_module_state(self):
        m = self._broker_client_module
        m._smartapi_consecutive_rate_limit_exhaustions = 0
        m._smartapi_cooldown_until = 0.0
        m._SMARTAPI_LAST_REQUEST_AT = 0.0

    def test_a_permanently_hung_call_raises_instead_of_blocking_forever(self):
        import threading
        import time as real_time

        from apps.market_data import broker_client
        from apps.market_data.broker_client import BrokerClient

        client = BrokerClient()
        original_timeout = broker_client._SMARTAPI_REQUEST_TIMEOUT_SECONDS
        broker_client._SMARTAPI_REQUEST_TIMEOUT_SECONDS = 0.2
        self.addCleanup(setattr, broker_client, "_SMARTAPI_REQUEST_TIMEOUT_SECONDS", original_timeout)

        # threading.Event().wait(), NOT time.sleep() -- this class's own
        # setUp monkey-patches broker_client.time.sleep to a no-op, but
        # `time` is one shared module object for the whole process (there
        # is no per-module copy), so that patch silently neuters EVERY
        # time.sleep() call anywhere, including one made here to simulate
        # a hang -- a real, first-draft bug in this exact test (it passed
        # "successfully" for the wrong reason: the simulated hang never
        # actually hung). Event.wait() is a separate blocking primitive
        # unaffected by that patch, so it genuinely blocks the calling
        # thread the way a real stuck network call would.
        never_set = threading.Event()

        def hangs_forever():
            never_set.wait(30)  # deliberately far longer than the 0.2s bound above
            return {"status": True}

        started = real_time.monotonic()
        with self.assertRaises(TimeoutError):
            client._smartapi_request(hangs_forever, retry_rate_limits=False)
        elapsed = real_time.monotonic() - started

        self.assertLess(elapsed, 5, "a hung call must be bounded by the hard timeout, not actually wait for the real work to finish")

    def test_shared_slot_wait_gives_up_after_its_own_bound_instead_of_hanging_forever(self):
        import time as real_time

        from apps.market_data import broker_client

        original_timeout = broker_client._SMARTAPI_SHARED_SLOT_WAIT_TIMEOUT_SECONDS
        broker_client._SMARTAPI_SHARED_SLOT_WAIT_TIMEOUT_SECONDS = 0.3
        self.addCleanup(setattr, broker_client, "_SMARTAPI_SHARED_SLOT_WAIT_TIMEOUT_SECONDS", original_timeout)

        with override_settings(SMARTAPI_DISTRIBUTED_RATE_LIMIT=True):
            # Simulates the slot being continuously held by another
            # process: cache.add always reports "already exists".
            with patch.object(broker_client.cache, "add", return_value=False):
                started = real_time.monotonic()
                broker_client._wait_for_smartapi_slot()  # must return, not hang
                elapsed = real_time.monotonic() - started

        self.assertLess(elapsed, 5, "must give up on the shared slot after its own bound, not loop forever")


class BrokerOrderSubmissionSafetyTests(TestCase):
    @override_settings(LIVE_TRADING_ENABLED=False)
    def test_disarmed_order_is_rejected_before_connecting(self):
        from apps.market_data.broker_client import BrokerClient

        client = BrokerClient()
        with patch.object(client, "_connect") as connect:
            with self.assertRaisesRegex(PermissionError, "disarmed"):
                client.place_order(
                    "NIFTYTESTCE", "BUY", 25,
                    symbol_token="token", exchange="NFO",
                    tradingsymbol="NIFTYTESTCE", order_tag="stable-tag",
                )

        connect.assert_not_called()

    @override_settings(LIVE_TRADING_ENABLED=True, ALLOW_LIVE_ORDERS=True)
    def test_state_changing_submission_is_called_once_without_rate_limit_retry(self):
        from apps.market_data.broker_client import BrokerClient

        class FakeConnection:
            def placeOrder(self, params):
                raise AssertionError("full-response order method should be preferred")

            def placeOrderFullResponse(self, params):
                return {"status": True, "data": {"orderid": "ORDER123"}}

        client = BrokerClient()
        with patch.object(client, "_connect", return_value=FakeConnection()), patch.object(
            client, "_smartapi_request",
            return_value={"status": True, "data": {"orderid": "ORDER123"}},
        ) as request:
            order_id = client.place_order(
                "NIFTYTESTCE", "BUY", 25,
                symbol_token="token", exchange="NFO",
                tradingsymbol="NIFTYTESTCE", order_tag="stable-tag",
            )

        self.assertEqual(order_id, "ORDER123")
        request.assert_called_once()
        self.assertIs(request.call_args.kwargs["retry_rate_limits"], False)
        params = request.call_args.args[1]
        self.assertEqual(params["ordertag"], "stable-tag")
        self.assertEqual(params["exchange"], "NFO")


class VwapTests(TestCase):
    """
    apps.market_data.vwap -- real cumulative(typical_price*volume)/
    cumulative(volume) over explicit, hand-placed intraday candles
    (not timezone.now()-relative, so the test doesn't depend on
    whether it happens to run during real NSE market hours).
    """

    def setUp(self):
        from datetime import datetime, time

        from .market_hours import MARKET_OPEN_TIME

        self.today = timezone.localdate()
        self.session_start = timezone.make_aware(datetime.combine(self.today, MARKET_OPEN_TIME))

    def _candle(self, minutes_after_open, high, low, close, volume):
        return HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m",
            timestamp=self.session_start + timedelta(minutes=minutes_after_open),
            open=Decimal(str(close)), high=Decimal(str(high)), low=Decimal(str(low)),
            close=Decimal(str(close)), volume=volume, source="test",
        )

    def test_vwap_matches_hand_computed_value(self):
        from .vwap import calculate_vwap

        # Two candles, typical price (h+l+c)/3:
        #   candle1: (101+99+100)/3 = 100.0, volume 1000
        #   candle2: (106+104+105)/3 = 105.0, volume 2000
        # VWAP = (100*1000 + 105*2000) / 3000 = 310000/3000 = 103.3333
        self._candle(0, 101, 99, 100, 1000)
        self._candle(5, 106, 104, 105, 2000)

        vwap = calculate_vwap("NIFTY", "5m", for_date=self.today)
        self.assertAlmostEqual(vwap, 103.3333, places=3)

    def test_vwap_none_with_no_candles(self):
        from .vwap import calculate_vwap

        self.assertIsNone(calculate_vwap("NIFTY", "5m", for_date=self.today))

    def test_vwap_ignores_zero_volume_candles(self):
        from .vwap import calculate_vwap

        self._candle(0, 101, 99, 100, 1000)
        self._candle(5, 999, 999, 999, 0)  # would badly skew VWAP if counted

        vwap = calculate_vwap("NIFTY", "5m", for_date=self.today)
        self.assertAlmostEqual(vwap, 100.0, places=3)

    def test_vwap_with_bands_reports_candle_count_and_symmetric_bands(self):
        from .vwap import calculate_vwap_with_bands

        self._candle(0, 101, 99, 100, 1000)
        self._candle(5, 106, 104, 105, 1000)

        result = calculate_vwap_with_bands("NIFTY", "5m", for_date=self.today, num_std=1.0)
        self.assertEqual(result["candle_count"], 2)
        self.assertIsNotNone(result["vwap"])
        # Equal volumes -> bands are symmetric around VWAP.
        self.assertAlmostEqual(result["vwap"] - result["lower_band"], result["upper_band"] - result["vwap"], places=6)


class MultiTimeframeRegimeTests(TestCase):
    """
    apps.market_data.multi_timeframe_regime -- tests the COMBINATION
    logic specifically, by mocking compute_indicators to return
    controlled per-timeframe readings (same boundary this codebase
    already mocks at elsewhere, e.g. apps/options/tests.py patching
    classify_regime directly) rather than fighting real ADX/BB-width
    arithmetic from synthetic candles, which nothing else in this test
    suite attempts either.
    """

    def _ind(self, ema9_slope=0.0, ema21_slope=0.0, adx=15.0, bb_width=0.03, rsi=50.0):
        return {"close": 100.0, "ema9_slope": ema9_slope, "ema21_slope": ema21_slope, "adx": adx, "bb_width": bb_width, "rsi": rsi, "atr": 1.0, "relative_volume": 1.0}

    def _patch_indicators(self, per_tf: dict):
        from unittest.mock import patch

        from apps.market_data import multi_timeframe_regime as mtr

        def fake_compute_indicators(symbol, tf):
            return per_tf.get(tf)

        return patch.object(mtr, "compute_indicators", side_effect=fake_compute_indicators)

    def test_majority_trending_bullish(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        ind = self._ind(ema9_slope=1.0, ema21_slope=0.5, adx=30.0)
        with self._patch_indicators({"5m": ind, "15m": ind, "1h": ind, "1d": ind}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m", "1h", "1d"))
        self.assertEqual(result["regime"], "TRENDING_BULLISH")
        self.assertEqual(result["unavailable_timeframes"], [])

    def test_majority_trending_bearish(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        ind = self._ind(ema9_slope=-1.0, ema21_slope=-0.5, adx=30.0)
        with self._patch_indicators({"5m": ind, "15m": ind, "1h": ind, "1d": ind}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m", "1h", "1d"))
        self.assertEqual(result["regime"], "TRENDING_BEARISH")

    def test_sideways_low_bb_width_is_low_volatility(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        ind = self._ind(adx=10.0, bb_width=0.005, rsi=50.0)
        with self._patch_indicators({"5m": ind, "15m": ind, "1h": ind, "1d": ind}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m", "1h", "1d"))
        self.assertEqual(result["regime"], "LOW_VOLATILITY")

    def test_sideways_extreme_rsi_is_mean_reversion(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        ind = self._ind(adx=10.0, bb_width=0.03, rsi=72.0)
        with self._patch_indicators({"5m": ind, "15m": ind, "1h": ind, "1d": ind}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m", "1h", "1d"))
        self.assertEqual(result["regime"], "MEAN_REVERSION")

    def test_sideways_normal_rsi_is_plain_sideways(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        ind = self._ind(adx=10.0, bb_width=0.03, rsi=50.0)
        with self._patch_indicators({"5m": ind, "15m": ind, "1h": ind, "1d": ind}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m", "1h", "1d"))
        self.assertEqual(result["regime"], "SIDEWAYS")

    def test_high_volatility_confirmed_breakout(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        base = timezone.now() - timedelta(days=10)
        for i in range(9):
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="1d", timestamp=base + timedelta(days=i),
                open=100, high=105, low=95, close=100, volume=10000, source="test",
            )
        # Today's close breaks well above every prior day's high.
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="1d", timestamp=timezone.now(),
            open=104, high=112, low=103, close=110, volume=20000, source="test",
        )

        ind = self._ind(adx=15.0, bb_width=0.08)  # high-vol (>= 0.06), not extreme enough for EVENT_DRIVEN (>= 0.12)
        with self._patch_indicators({"5m": ind, "15m": ind, "1h": ind, "1d": ind}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m", "1h", "1d"))
        self.assertEqual(result["regime"], "BREAKOUT")

    def test_high_volatility_no_breakout_stays_high_volatility(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        base = timezone.now() - timedelta(days=10)
        for i in range(10):
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="1d", timestamp=base + timedelta(days=i),
                open=100, high=106, low=94, close=100 + (i % 3), volume=10000, source="test",
            )
        ind = self._ind(adx=15.0, bb_width=0.08)
        with self._patch_indicators({"5m": ind, "15m": ind, "1h": ind, "1d": ind}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m", "1h", "1d"))
        self.assertEqual(result["regime"], "HIGH_VOLATILITY")

    def test_high_volatility_extreme_synchronized_is_event_driven(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        # No HistoricalData seeded at all -> breakout/breakdown check can't run (falls through to the bb_width check).
        ind = self._ind(adx=15.0, bb_width=0.15)  # >= BB_WIDTH_HIGH_VOL_THRESHOLD(0.06) * 2.0 = 0.12
        with self._patch_indicators({"5m": ind, "15m": ind, "1h": ind, "1d": ind}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m", "1h", "1d"))
        self.assertEqual(result["regime"], "EVENT_DRIVEN")
        self.assertLessEqual(result["confidence"], 0.3)

    def test_no_majority_is_undefined(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        trending = self._ind(ema9_slope=1.0, ema21_slope=0.5, adx=30.0, bb_width=0.03)
        sideways = self._ind(adx=10.0, bb_width=0.03, rsi=50.0)
        high_vol = self._ind(adx=10.0, bb_width=0.08)
        with self._patch_indicators({"5m": trending, "15m": sideways, "1h": high_vol}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m", "1h"))
        self.assertEqual(result["regime"], "UNDEFINED")

    def test_no_data_anywhere_is_undefined_with_zero_confidence(self):
        from apps.market_data.multi_timeframe_regime import classify_composite_regime

        with self._patch_indicators({}):
            result = classify_composite_regime("NIFTY", timeframes=("5m", "15m"))
        self.assertEqual(result["regime"], "UNDEFINED")
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(set(result["unavailable_timeframes"]), {"5m", "15m"})


class TimeOfDayTests(TestCase):
    """apps.market_data.time_of_day -- session-phase classification + real historical-baseline volatility comparison."""

    def setUp(self):
        from datetime import date as date_cls

        self.monday = date_cls(2026, 3, 2)  # a real, confirmed weekday, not an NSE_HOLIDAYS date

    def _at(self, hour, minute):
        from datetime import datetime as dt_cls
        from datetime import time as time_cls

        return timezone.make_aware(dt_cls.combine(self.monday, time_cls(hour, minute)))

    def test_calculate_time_of_day_regime_maps_known_windows(self):
        from .time_of_day import calculate_time_of_day_regime

        self.assertEqual(calculate_time_of_day_regime(self._at(9, 20))["phase"], "opening")
        self.assertEqual(calculate_time_of_day_regime(self._at(10, 30))["phase"], "morning")
        self.assertEqual(calculate_time_of_day_regime(self._at(12, 0))["phase"], "midday")
        self.assertEqual(calculate_time_of_day_regime(self._at(14, 0))["phase"], "afternoon")
        self.assertEqual(calculate_time_of_day_regime(self._at(15, 15))["phase"], "closing")
        self.assertEqual(calculate_time_of_day_regime(self._at(8, 0))["phase"], "closed")

    def _seed_daily_candle_with_opening_range(self, day, opening_high, opening_low):
        from datetime import datetime as dt_cls, time as time_cls

        window_start = timezone.make_aware(dt_cls.combine(day, time_cls(9, 20)))
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=window_start,
            open=opening_low, high=opening_high, low=opening_low, close=opening_high,
            volume=10000, source="test",
        )

    def test_detect_opening_volatility_elevated_vs_baseline(self):
        from datetime import timedelta as td

        from .time_of_day import detect_opening_volatility

        # 5 baseline days: a small, consistent opening range (~1% of price).
        for i in range(1, 6):
            self._seed_daily_candle_with_opening_range(self.monday - td(days=i), opening_high=24520, opening_low=24500)
        # "Today" (the Monday itself): a much wider opening range (~5%).
        self._seed_daily_candle_with_opening_range(self.monday, opening_high=25725, opening_low=24500)

        with patch_localdate(self.monday):
            result = detect_opening_volatility("NIFTY", "5m", lookback_days=10)
        self.assertEqual(result["state"], "elevated")
        self.assertGreater(result["ratio"], 1.3)

    def test_detect_opening_volatility_unavailable_without_history(self):
        from .time_of_day import detect_opening_volatility

        result = detect_opening_volatility("NIFTY", "5m")
        self.assertEqual(result["state"], "unavailable")


def patch_localdate(fixed_date):
    """Small context manager: pins timezone.localdate() to a fixed date for time-of-day baseline tests, which otherwise depend on whenever the test suite happens to run."""
    from contextlib import contextmanager
    from unittest.mock import patch

    @contextmanager
    def _cm():
        with patch.object(timezone, "localdate", return_value=fixed_date):
            yield

    return _cm()


class EventRiskTests(TestCase):
    """apps.market_data.event_risk -- real options-expiry-day flag, honestly-unavailable macro event risk."""

    def test_reports_real_expiry_flag_and_unavailable_macro_risk(self):
        from apps.options.models import OptionContract

        from .event_risk import detect_event_risk

        OptionContract.objects.create(
            underlying="NIFTY", expiry=timezone.localdate(), strike=24500, option_type="CE",
            symbol_token="tok_expiry_today", tradingsymbol="NIFTYTODAYCE", lot_size=25,
        )
        result = detect_event_risk("NIFTY")
        self.assertTrue(result["available"])
        self.assertTrue(result["is_options_expiry_day"])
        self.assertEqual(result["macro_event_risk"], "unavailable")

    def test_no_expiry_today(self):
        from .event_risk import detect_event_risk

        result = detect_event_risk("BANKNIFTY")
        self.assertFalse(result["is_options_expiry_day"])
