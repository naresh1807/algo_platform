from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.risk.models import AccountEquity
from apps.signals.models import TradingSignal

from .data_quality import DataQualityReport
from .metrics import compute_max_pain, compute_pcr, strike_support_resistance
from .models import OptionChainSnapshot, OptionContract


def _ist(year, month, day, hour=10, minute=0):
    """
    Builds an aware Asia/Kolkata datetime for expiry-service tests --
    same "accept an explicit `at` for deterministic testing" convention
    apps.market_data.market_hours.is_market_open already established
    (see that module's own docstring), so none of these tests depend on
    when they're actually run and no time-freezing library is needed
    (none is installed in this project).
    """
    return timezone.make_aware(datetime(year, month, day, hour, minute), timezone.get_default_timezone())


class ExpiryServiceTests(TestCase):
    """
    apps.options.expiry_service -- the single shared expiry-resolution
    service. Covers the platform's real expiry-lifecycle requirements:
    cutoff-aware rollover on expiry day itself, weekend/holiday
    handling, month/year boundaries, multi-expiry selection, and the
    UTC/IST timezone boundary (settings.OPTIONS_EXPIRY_CUTOFF_TIME
    defaults to 15:30 IST, confirmed via config/settings.py).
    """

    UNDERLYING = "TESTIDX"

    def _make_contract(self, expiry, strike=100, symbol_token=None):
        return OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=expiry, strike=strike, option_type="CE",
            symbol_token=symbol_token or f"tok_{expiry.isoformat()}_{strike}",
        )

    # 1. Normal non-expiry trading day.
    def test_normal_trading_day_expiry_in_future_is_eligible(self):
        from .expiry_service import is_expiry_eligible

        at = _ist(2026, 8, 18, 11, 0)  # a Tuesday
        self.assertTrue(is_expiry_eligible(date(2026, 8, 21), at=at))

    # 2. Expiry day before market close.
    def test_expiry_day_before_cutoff_is_still_eligible(self):
        from .expiry_service import is_expiry_eligible

        at = _ist(2026, 8, 21, 14, 59)
        self.assertTrue(is_expiry_eligible(date(2026, 8, 21), at=at))

    # 3. Expiry day immediately after market close.
    def test_expiry_day_after_cutoff_rolls_over(self):
        from .expiry_service import is_expiry_eligible, resolve_current_expiry

        at = _ist(2026, 8, 21, 15, 31)
        self.assertFalse(is_expiry_eligible(date(2026, 8, 21), at=at))

        self._make_contract(date(2026, 8, 21))
        self._make_contract(date(2026, 8, 28))
        self.assertEqual(resolve_current_expiry(self.UNDERLYING, at=at), date(2026, 8, 28))

    # 4. Day after expiry.
    def test_day_after_expiry_is_never_eligible_regardless_of_time(self):
        from .expiry_service import is_expiry_eligible

        at_morning = _ist(2026, 8, 22, 9, 0)
        at_evening = _ist(2026, 8, 22, 20, 0)
        self.assertFalse(is_expiry_eligible(date(2026, 8, 21), at=at_morning))
        self.assertFalse(is_expiry_eligible(date(2026, 8, 21), at=at_evening))

    # 5. Weekend.
    def test_weekend_resolves_to_nearest_future_expiry(self):
        from .expiry_service import resolve_current_expiry

        saturday = _ist(2026, 8, 22, 11, 0)  # Aug 22 2026 is a Saturday
        self._make_contract(date(2026, 8, 27))  # next Thursday
        self.assertEqual(resolve_current_expiry(self.UNDERLYING, at=saturday), date(2026, 8, 27))

    # 6. Month-end and year-end rollover.
    def test_month_end_rollover(self):
        from .expiry_service import is_expiry_eligible

        at = _ist(2026, 8, 31, 16, 0)  # after cutoff, last day of August
        self.assertFalse(is_expiry_eligible(date(2026, 8, 31), at=at))
        self.assertTrue(is_expiry_eligible(date(2026, 9, 1), at=at))

    def test_year_end_rollover(self):
        from .expiry_service import is_expiry_eligible

        at = _ist(2027, 1, 1, 9, 0)  # New Year's Day
        self.assertFalse(is_expiry_eligible(date(2026, 12, 31), at=at))
        self.assertTrue(is_expiry_eligible(date(2027, 1, 7), at=at))

    # 7. Exchange-holiday-adjusted expiry supplied by the instrument master.
    def test_non_thursday_holiday_shifted_expiry_handled_by_real_date_only(self):
        """
        No weekday assumption anywhere in the service -- a real NSE
        holiday can shift a weekly expiry off its usual day (e.g. to a
        Wednesday); this must be handled purely by comparing the actual
        date given, never by asserting/expecting a specific weekday.
        """
        from .expiry_service import is_expiry_eligible

        wednesday_expiry = date(2026, 8, 19)
        self.assertEqual(wednesday_expiry.weekday(), 2)  # sanity: this really is a Wednesday
        self.assertTrue(is_expiry_eligible(wednesday_expiry, at=_ist(2026, 8, 18, 12, 0)))
        self.assertTrue(is_expiry_eligible(wednesday_expiry, at=_ist(2026, 8, 19, 10, 0)))
        self.assertFalse(is_expiry_eligible(wednesday_expiry, at=_ist(2026, 8, 19, 16, 0)))
        self.assertFalse(is_expiry_eligible(wednesday_expiry, at=_ist(2026, 8, 20, 9, 0)))

    # 8. Multiple upcoming expiries.
    def test_multiple_expiries_next_week_mode(self):
        from .expiry_service import list_eligible_expiries, resolve_current_expiry

        at = _ist(2026, 8, 18, 11, 0)
        for offset in (3, 10, 17, 24):
            self._make_contract(date(2026, 8, 18) + timedelta(days=offset))

        eligible = list_eligible_expiries(self.UNDERLYING, at=at)
        self.assertEqual(len(eligible), 4)
        self.assertEqual(eligible, sorted(eligible))
        self.assertEqual(resolve_current_expiry(self.UNDERLYING, mode="current_week", at=at), eligible[0])
        self.assertEqual(resolve_current_expiry(self.UNDERLYING, mode="next_week", at=at), eligible[1])

    # 9. Only one expiry initially stored.
    def test_only_one_expiry_stored_next_week_mode_returns_none(self):
        from .expiry_service import resolve_current_expiry

        at = _ist(2026, 8, 18, 11, 0)
        self._make_contract(date(2026, 8, 21))
        self.assertEqual(resolve_current_expiry(self.UNDERLYING, mode="current_week", at=at), date(2026, 8, 21))
        self.assertIsNone(resolve_current_expiry(self.UNDERLYING, mode="next_week", at=at))

    # 10 (empty-database case belongs to ContractSyncTests below, but the
    # service's own "nothing synced" behavior is covered here too).
    def test_no_contracts_synced_returns_none(self):
        from .expiry_service import resolve_current_expiry

        self.assertIsNone(resolve_current_expiry("NEVER_SYNCED", at=_ist(2026, 8, 18)))

    def test_rollover_required_true_when_buffer_thin(self):
        from .expiry_service import rollover_required

        at = _ist(2026, 8, 18, 11, 0)
        self._make_contract(date(2026, 8, 21))
        with override_settings(OPTIONS_EXPIRY_SYNC_COUNT=4):
            self.assertTrue(rollover_required(self.UNDERLYING, at=at))

    def test_rollover_required_false_when_enough_eligible_expiries(self):
        from .expiry_service import rollover_required

        at = _ist(2026, 8, 18, 11, 0)
        for offset in (3, 10, 17, 24):
            self._make_contract(date(2026, 8, 18) + timedelta(days=offset))
        with override_settings(OPTIONS_EXPIRY_SYNC_COUNT=4):
            self.assertFalse(rollover_required(self.UNDERLYING, at=at))

    def test_validate_requested_expiry_falls_back_when_expired(self):
        from .expiry_service import validate_requested_expiry

        at = _ist(2026, 8, 22, 11, 0)
        self._make_contract(date(2026, 8, 21))  # already expired relative to `at`
        self._make_contract(date(2026, 8, 28))
        resolved, substituted = validate_requested_expiry(self.UNDERLYING, date(2026, 8, 21), at=at)
        self.assertEqual(resolved, date(2026, 8, 28))
        self.assertTrue(substituted)

    def test_validate_requested_expiry_honors_still_valid_request(self):
        from .expiry_service import validate_requested_expiry

        at = _ist(2026, 8, 18, 11, 0)
        self._make_contract(date(2026, 8, 21))
        self._make_contract(date(2026, 8, 28))
        resolved, substituted = validate_requested_expiry(self.UNDERLYING, date(2026, 8, 28), at=at)
        self.assertEqual(resolved, date(2026, 8, 28))
        self.assertFalse(substituted)

    # 20. Timezone boundary between UTC and Asia/Kolkata.
    def test_utc_ist_boundary_crosses_calendar_date(self):
        """
        2026-08-20 19:00 UTC is 2026-08-21 00:30 IST -- the SAME instant
        falls on two different calendar dates depending on which
        timezone you ask in. is_expiry_eligible must use the IST date
        (Asia/Kolkata, settings.TIME_ZONE), not the UTC date, or a
        contract would flip eligibility 5.5 hours "early" by UTC clocks.
        """
        from .expiry_service import is_expiry_eligible

        utc_moment = timezone.make_aware(datetime(2026, 8, 20, 19, 0), timezone.UTC)
        # In IST this is already 2026-08-21 00:30 -- Aug 20 is now a
        # fully past date in IST terms, Aug 21 is "today."
        self.assertFalse(is_expiry_eligible(date(2026, 8, 20), at=utc_moment))
        self.assertTrue(is_expiry_eligible(date(2026, 8, 21), at=utc_moment))  # early morning, well before cutoff


class OptionsMetricsTests(TestCase):
    def setUp(self):
        self.expiry = date.today() + timedelta(days=7)
        self.underlying = "NIFTY"

        # Two strikes, CE + PE each, with distinct OI so PCR/max-pain/
        # support-resistance all have something non-trivial to compute.
        self.ce_24500 = OptionContract.objects.create(
            underlying=self.underlying, expiry=self.expiry, strike=24500,
            option_type="CE", symbol_token="tok_ce_24500",
        )
        self.pe_24500 = OptionContract.objects.create(
            underlying=self.underlying, expiry=self.expiry, strike=24500,
            option_type="PE", symbol_token="tok_pe_24500",
        )
        self.ce_24600 = OptionContract.objects.create(
            underlying=self.underlying, expiry=self.expiry, strike=24600,
            option_type="CE", symbol_token="tok_ce_24600",
        )

        now = timezone.now()
        OptionChainSnapshot.objects.create(
            contract=self.ce_24500, timestamp=now, ltp=120, open_interest=1000, change_in_oi=0,
        )
        OptionChainSnapshot.objects.create(
            contract=self.pe_24500, timestamp=now, ltp=80, open_interest=3000, change_in_oi=0,
        )
        OptionChainSnapshot.objects.create(
            contract=self.ce_24600, timestamp=now, ltp=60, open_interest=2000, change_in_oi=0,
        )

    def test_pcr_computed_from_latest_snapshots(self):
        # put OI (3000) / call OI (1000 + 2000 = 3000) = 1.0
        pcr = compute_pcr(self.underlying, self.expiry)
        self.assertEqual(pcr, 1.0)

    def test_pcr_none_with_no_data(self):
        self.assertIsNone(compute_pcr("BANKNIFTY", self.expiry))

    def test_max_pain_returns_a_listed_strike(self):
        max_pain = compute_max_pain(self.underlying, self.expiry)
        self.assertIn(max_pain, [24500.0, 24600.0])

    def test_support_resistance_shape(self):
        result = strike_support_resistance(self.underlying, self.expiry)
        self.assertEqual(result["support"][0]["strike"], 24500.0)
        self.assertEqual(result["resistance"][0]["strike"], 24600.0)


class LatestSnapshotsCorrelationTests(TestCase):
    """
    Regression test for apps.options.metrics._latest_snapshots' correlated
    subquery -- guards against reintroducing the OuterRef("pk") bug
    (correlating each candidate row against its OWN id instead of its
    contract_id), which silently returned zero rows for any contract
    that had accumulated more than one snapshot over time -- i.e. any
    real dataset, ever (see metrics.py's fix comment for the mechanism).
    Deliberately gives ONE contract TWO snapshots so the latest one's
    own pk can never coincidentally equal the contract's pk the way a
    single-snapshot-per-contract fixture could mask this exact bug --
    that's exactly the fixture shape OptionsMetricsTests below already
    had, which is why this bug shipped without any test catching it.
    """

    def test_returns_the_latest_snapshot_per_contract_not_the_first(self):
        from .metrics import _latest_snapshots

        expiry = date.today() + timedelta(days=7)
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24500,
            option_type="CE", symbol_token="tok_multi",
        )
        older = timezone.now() - timedelta(minutes=10)
        newer = timezone.now()
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=older, ltp=100, open_interest=1000, change_in_oi=0,
        )
        latest = OptionChainSnapshot.objects.create(
            contract=contract, timestamp=newer, ltp=150, open_interest=2000, change_in_oi=1000,
        )

        result = list(_latest_snapshots("NIFTY", expiry))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].pk, latest.pk)
        self.assertEqual(result[0].open_interest, 2000)


class SuggestBestStrikeTests(TestCase):
    """
    apps.options.strike_selector.suggest_best_strike -- specifically that
    a winning candidate's dict carries "contract_id", which is what
    apps.options.index_direction_strategy uses to load the real
    OptionContract row for real-option execution (see that module's
    docstring) instead of a second (underlying, expiry, strike,
    option_type) lookup.
    """

    def test_suggested_candidate_carries_contract_id(self):
        from apps.market_data.models import HistoricalData

        from .strike_selector import suggest_best_strike

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500,
            volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400,
            option_type="CE", symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE",
            lot_size=25,
        )
        # ltp chosen so its Black-Scholes-implied delta (~0.59) lands
        # inside strike_selector.DELTA_SWEET_SPOT (0.35-0.65) -- see
        # GreeksTests above for how this module already checks the
        # underlying math; this fixture just needs ONE real candidate to
        # clear that filter so "suggested" is non-null.
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=timezone.now(),
            ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000,
        )

        result = suggest_best_strike("NIFTY", expiry, direction="bullish")

        self.assertIsNotNone(result["suggested"])
        self.assertEqual(result["suggested"]["contract_id"], contract.pk)
        self.assertEqual(result["suggested"]["strike"], 24400.0)


class GreeksTests(TestCase):
    """
    Pure-math tests (no DB) for apps.options.greeks -- checked against
    known Black-Scholes reference values.
    """

    def test_atm_call_price_matches_known_value(self):
        from .greeks import black_scholes_price
        # spot=100, strike=100, T=0.25y, r=5%, sigma=20% -> ~4.615 (textbook value)
        price = black_scholes_price(100, 100, 0.25, 0.05, 0.20, "CE")
        self.assertAlmostEqual(price, 4.615, places=2)

    def test_implied_volatility_recovers_input_sigma(self):
        from .greeks import black_scholes_price, implied_volatility
        price = black_scholes_price(100, 100, 0.25, 0.05, 0.20, "CE")
        iv = implied_volatility(price, 100, 100, 0.25, 0.05, "CE")
        self.assertAlmostEqual(iv, 0.20, places=3)

    def test_call_delta_between_zero_and_one(self):
        from .greeks import compute_greeks
        greeks = compute_greeks(100, 100, 0.25, 0.05, 0.20, "CE")
        self.assertTrue(0.0 < greeks["delta"] < 1.0)

    def test_put_delta_between_minus_one_and_zero(self):
        from .greeks import compute_greeks
        greeks = compute_greeks(100, 100, 0.25, 0.05, 0.20, "PE")
        self.assertTrue(-1.0 < greeks["delta"] < 0.0)

    def test_degenerate_inputs_return_none(self):
        from .greeks import black_scholes_price, compute_greeks
        self.assertIsNone(black_scholes_price(100, 100, 0, 0.05, 0.20, "CE"))
        self.assertIsNone(compute_greeks(100, 100, 0.25, 0.05, 0, "CE"))


class OptionExpiriesViewTests(APITestCase):
    """
    /api/options/expiries/ -- must only offer current-or-future
    expiries, since the frontend auto-selects this list's first entry
    as the default option chain to display (see the view's own
    docstring for the bug this guards: an expired expiry sorting first
    meant the page defaulted to a dead chain).
    """

    # A different underlying from OptionsMetricsTests' fixtures (which
    # use NIFTY at date.today() + 7 days) so the two test classes' rows
    # can never collide on OptionContract's (underlying, expiry, strike,
    # option_type) unique_together, even though each runs in its own
    # rolled-back transaction and shouldn't observe the other's data at
    # all.
    UNDERLYING = "BANKNIFTY"

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Trader")[0])
        self.client.force_authenticate(self.user)

        self.expired = date.today() - timedelta(days=1)
        self.current = date.today() + timedelta(days=3)
        self.later = date.today() + timedelta(days=10)

        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=self.expired, strike=51000,
            option_type="CE", symbol_token="tok_expired",
        )
        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=self.current, strike=51500,
            option_type="CE", symbol_token="tok_current",
        )
        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=self.later, strike=51500,
            option_type="CE", symbol_token="tok_later",
        )

    def test_expired_expiry_excluded_and_nearest_is_first(self):
        response = self.client.get("/api/options/expiries/", {"underlying": self.UNDERLYING})
        self.assertEqual(response.status_code, 200)
        expiries = response.data["expiries"]
        self.assertNotIn(self.expired.isoformat(), expiries)
        self.assertEqual(expiries[0], self.current.isoformat())
        self.assertEqual(expiries, [self.current.isoformat(), self.later.isoformat()])


class IndexDirectionStrategyTests(TestCase):
    """apps.options.index_direction_strategy -- see that module's own docstring for scope."""

    def test_determine_index_direction_returns_none_with_no_data(self):
        from .index_direction_strategy import determine_index_direction

        result = determine_index_direction("NIFTY", "5m")
        self.assertIsNone(result["direction"])
        self.assertIsNone(result["option_side"])
        self.assertIn("Not enough historical candles", result["detail"])

    def test_success_rate_for_side_not_available_with_no_data(self):
        from .index_direction_strategy import success_rate_for_side

        result = success_rate_for_side("NIFTY", "up", "5m")
        self.assertFalse(result["available"])
        self.assertFalse(result["profitable"])

    def test_success_rate_for_side_not_available_below_min_trades(self):
        """
        Flat, directionless candles never clear DIRECTION_SCORE_THRESHOLD,
        so the bootstrap backtest finds zero simulated trades -- fewer
        than MIN_BOOTSTRAP_TRADES, so a success rate isn't reported as
        available (not enough samples to mean anything).
        """
        from apps.market_data.models import HistoricalData

        from .index_direction_strategy import success_rate_for_side

        now = timezone.now()
        for i in range(120, 0, -1):
            close = 24500 + (i % 2)  # tiny alternating noise, no real trend
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="5m", timestamp=now - timedelta(minutes=5 * i),
                open=close, high=close + 1, low=close - 1, close=close,
                volume=100000, source="test",
            )

        result = success_rate_for_side("NIFTY", "up", "5m")
        self.assertFalse(result["available"])
        self.assertLess(result["trade_count"], 5)

    def test_evaluate_index_direction_trade_logs_no_trade_with_no_data(self):
        from apps.signals.models import TradingSignal
        from common.constants import SignalStatus, SignalType

        from .index_direction_strategy import evaluate_index_direction_trade

        signal = evaluate_index_direction_trade("NIFTY", "5m")
        self.assertEqual(signal.signal_type, SignalType.NO_TRADE)
        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertIn("Not enough historical candles", signal.reason)
        self.assertEqual(TradingSignal.objects.count(), 1)

    def test_evaluate_index_direction_trade_always_returns_a_saved_row(self):
        """
        Same "whatever the outcome, a real row must exist" property
        GenerateSignalTests checks for apps.signals.engine.generate_signal
        -- this strategy makes the exact same "log every decision" promise.
        """
        from apps.market_data.models import HistoricalData
        from apps.risk.models import AccountEquity
        from common.constants import SignalType

        from .index_direction_strategy import evaluate_index_direction_trade

        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )
        now = timezone.now()
        price = 24500.0
        for i in range(120, 0, -1):
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="5m", timestamp=now - timedelta(minutes=5 * i),
                open=price, high=price + 5, low=price - 5, close=price + (i % 3),
                volume=100000, source="test",
            )

        signal = evaluate_index_direction_trade("NIFTY", "5m")
        self.assertIsNotNone(signal.pk)
        self.assertIn(signal.signal_type, [SignalType.BUY, SignalType.SELL, SignalType.NO_TRADE])


class EvaluateIndexDirectionTradeApprovalTests(TestCase):
    """
    evaluate_index_direction_trade's approved path -- specifically that
    it stamps option_side/strike_price onto the resulting TradingSignal,
    which is what the live dashboard/popup/signal-list pages now read to
    show "which strike, CE or PE" (see apps.signals.signals' WebSocket
    broadcast and the frontend Signals.jsx/Dashboard.jsx/
    SignalAlertPopup.jsx pages).

    Mocks every upstream gate (direction, success rate, sentiment,
    options confluence, strike suggestion) to force a deterministic
    approval -- each of those gates already has its own dedicated
    tests elsewhere in this file (or in apps.risk's own test suite for
    check_pre_trade, which is left un-mocked and runs for real against
    the seeded AccountEquity below) -- this test's only job is to check
    the wiring from "a real approval happened" to "the signal carries
    the right option_side/strike_price", not to re-prove any individual
    gate's own logic.
    """

    def setUp(self):
        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )

    def test_approved_pe_signal_carries_option_side_and_strike(self):
        from unittest.mock import patch

        from apps.signals.models import TradingSignal
        from common.constants import SignalStatus, SignalType

        from . import index_direction_strategy as strat

        fake_ind = {
            "close": 24500.0, "atr": 100.0, "rsi": 40, "adx": 30,
            "bb_width": 0.02, "relative_volume": 1.5, "ema9": 0, "ema21": 0,
            "ema9_slope": 0, "ema21_slope": 0, "macd_hist_prev": 0, "macd_hist": 0,
            "macd": 0, "macd_signal": 0, "sar": 0,
        }
        direction_result = {
            "direction": "down", "option_side": "PE", "score": 0.9,
            "regime": "trending", "ind": fake_ind, "detail": "forced bearish for test",
        }
        success = {
            "available": True, "trade_count": 10, "win_rate": 0.6,
            "expectancy_r": 0.3, "profit_factor": 1.8, "profitable": True,
            "detail": "forced profitable for test",
        }
        sentiment = {
            "sentiment_score": 0.1, "has_contradictory_headline": False,
            "has_strongly_positive_headline": False,
            "headline_count": 3, "confidence": 0.5,
        }
        options_result = {"score": 0.6, "veto": False, "veto_reason": "", "detail": "forced confirming for test"}
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=24400,
            option_type="PE", symbol_token="tok_pe_24400", tradingsymbol="NIFTY24400PE",
            lot_size=1,  # =1 so the mocked position_size below survives lot-rounding unchanged
        )
        suggestion = {
            "suggested": {
                "contract_id": contract.pk,
                "strike": 24400.0, "ltp": 110.0, "bid": 109.0, "ask": 111.0, "open_interest": 1000, "volume": 500,
                "delta": -0.4, "theta": -10, "vega": 5, "iv": 15, "in_sweet_spot": True, "score": 0.7,
            },
            "reason": "24400 PE test suggestion", "candidates": [],
        }
        # Sizing/exposure math is apps.risk's own concern (and already
        # covered by its own test suite) -- mocked here so this test
        # doesn't need to reverse-engineer a stop distance that happens
        # to clear MAX_ONE_SYMBOL_EXPOSURE_PCT for an index-level entry
        # price, which real ATR-based stops on a ~24500 instrument
        # would not do by default (a synthetic small ATR like this
        # fixture's produces a position sized well past the 2%
        # single-symbol exposure cap -- confirmed by running this test
        # unmocked, not a bug, just not what this test is checking).
        from apps.risk.engine import RiskDecision
        risk_decision = RiskDecision(approved=True, risk_score=1.0, reasons=[], position_size=5)

        with patch.object(strat, "determine_index_direction", return_value=direction_result), \
             patch.object(strat, "success_rate_for_side", return_value=success), \
             patch.object(strat, "aggregate_sentiment", return_value=sentiment), \
             patch.object(strat, "options_confluence_score", return_value=options_result), \
             patch.object(strat, "select_expiry", return_value=date.today() + timedelta(days=7)), \
             patch.object(strat, "validate_option_chain_snapshot", return_value=DataQualityReport(valid=True, status="DATA_VALID", issues=[])), \
             patch.object(strat, "suggest_best_strike", return_value=suggestion), \
             patch.object(strat, "check_pre_trade", return_value=risk_decision):
            signal = strat.evaluate_index_direction_trade("NIFTY", "5m")

        self.assertIsInstance(signal, TradingSignal)
        self.assertEqual(signal.status, SignalStatus.APPROVED)
        # Always BUY once a real contract resolves -- buying a PE is a LONG
        # bet on that PE's own premium, not a SELL/short (see
        # index_direction_strategy's module docstring).
        self.assertEqual(signal.signal_type, SignalType.BUY)
        self.assertEqual(signal.option_side, "PE")
        self.assertEqual(signal.strike_price, 24400.0)
        self.assertEqual(signal.option_contract_id, contract.pk)

    def _run_with_sentiment(self, direction: str, sentiment_extra: dict):
        """Shared plumbing for the direction-aware sentiment veto tests below."""
        from unittest.mock import patch

        from apps.risk.engine import RiskDecision

        from . import index_direction_strategy as strat

        fake_ind = {
            "close": 24500.0, "atr": 100.0, "rsi": 40, "adx": 30,
            "bb_width": 0.02, "relative_volume": 1.5, "ema9": 0, "ema21": 0,
            "ema9_slope": 0, "ema21_slope": 0, "macd_hist_prev": 0, "macd_hist": 0,
            "macd": 0, "macd_signal": 0, "sar": 0,
        }
        option_side = "CE" if direction == "up" else "PE"
        direction_result = {
            "direction": direction, "option_side": option_side, "score": 0.9,
            "regime": "trending", "ind": fake_ind, "detail": "forced for test",
        }
        success = {
            "available": True, "trade_count": 10, "win_rate": 0.6,
            "expectancy_r": 0.3, "profit_factor": 1.8, "profitable": True,
            "detail": "forced profitable for test",
        }
        sentiment = {
            "sentiment_score": 0.0, "has_contradictory_headline": False,
            "has_strongly_positive_headline": False, "headline_count": 3, "confidence": 0.5,
            **sentiment_extra,
        }
        options_result = {"score": 0.6, "veto": False, "veto_reason": "", "detail": "forced for test"}
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=24400,
            option_type=option_side, symbol_token=f"tok_{option_side.lower()}_24400",
            tradingsymbol=f"NIFTY24400{option_side}", lot_size=1,
        )
        suggestion = {
            "suggested": {
                "contract_id": contract.pk,
                "strike": 24400.0, "ltp": 110.0, "bid": 109.0, "ask": 111.0, "open_interest": 1000, "volume": 500,
                "delta": -0.4, "theta": -10, "vega": 5, "iv": 15, "in_sweet_spot": True, "score": 0.7,
            },
            "reason": "test suggestion", "candidates": [],
        }
        risk_decision = RiskDecision(approved=True, risk_score=1.0, reasons=[], position_size=5)

        with patch.object(strat, "determine_index_direction", return_value=direction_result), \
             patch.object(strat, "success_rate_for_side", return_value=success), \
             patch.object(strat, "aggregate_sentiment", return_value=sentiment), \
             patch.object(strat, "options_confluence_score", return_value=options_result), \
             patch.object(strat, "select_expiry", return_value=date.today() + timedelta(days=7)), \
             patch.object(strat, "validate_option_chain_snapshot", return_value=DataQualityReport(valid=True, status="DATA_VALID", issues=[])), \
             patch.object(strat, "suggest_best_strike", return_value=suggestion), \
             patch.object(strat, "check_pre_trade", return_value=risk_decision):
            return strat.evaluate_index_direction_trade("NIFTY", "5m")

    def test_negative_headline_does_not_block_pe_trade(self):
        """
        A negative headline contradicts a BULLISH thesis, not a bearish
        one -- vetoing a PE trade because of bearish news would be
        backwards (the news actually confirms the PE thesis).
        """
        from common.constants import SignalStatus

        signal = self._run_with_sentiment("down", {"has_contradictory_headline": True})
        self.assertEqual(signal.status, SignalStatus.APPROVED)
        self.assertEqual(signal.option_side, "PE")

    def test_positive_headline_blocks_pe_trade(self):
        from common.constants import SignalStatus

        signal = self._run_with_sentiment("down", {"has_strongly_positive_headline": True})
        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertIn("strongly positive", signal.reason)

    def test_positive_headline_does_not_block_ce_trade(self):
        from common.constants import SignalStatus

        signal = self._run_with_sentiment("up", {"has_strongly_positive_headline": True})
        self.assertEqual(signal.status, SignalStatus.APPROVED)
        self.assertEqual(signal.option_side, "CE")

    def test_negative_headline_blocks_ce_trade(self):
        from common.constants import SignalStatus

        signal = self._run_with_sentiment("up", {"has_contradictory_headline": True})
        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertIn("strongly negative", signal.reason)


class RunIndexDirectionStrategyTaskTests(TestCase):
    def test_skips_outside_market_hours(self):
        from unittest.mock import patch

        with patch("apps.options.tasks.is_market_open", return_value=(False, "market closed")):
            from .tasks import run_index_direction_strategy

            result = run_index_direction_strategy()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "market closed")


class IndexDirectionStrategyNanSafetyTests(TestCase):
    """
    A real gap in ingested candles (plausible on a rate-limited day --
    see apps.market_data.broker_client's circuit breaker) can leave an
    indicator like ATR as NaN for some bar. Every comparison against
    NaN is False in Python, so a guard written as "skip if bad" (e.g.
    `if x <= 0`) silently lets NaN through unguarded -- these tests
    pin down that the NaN-safe "proceed only if positive" phrasing
    (`if not (x > 0)`) actually catches it, at both the level this bug
    was FOUND at (success_rate_for_side's backtest bootstrap reporting
    "expectancy +nanR", making `profitable` structurally impossible to
    ever be True) and the live-signal path.
    """

    def test_simulate_directional_exit_treats_nan_risk_as_zero_not_nan(self):
        import pandas as pd

        from .index_direction_strategy import _simulate_directional_exit

        df = pd.DataFrame({"high": [100, 101, 102], "low": [99, 98, 97], "close": [100, 100, 100]})
        exit_index, r = _simulate_directional_exit(
            df, entry_index=0, entry_price=100.0, stop=float("nan"), target=105.0,
            direction="up", max_holding_bars=2,
        )
        self.assertEqual(r, 0.0)

    def test_r_multiples_skips_a_nan_atr_bar_instead_of_trading_it(self):
        from unittest.mock import patch

        import pandas as pd

        from . import index_direction_strategy as strat

        df = pd.DataFrame({"high": [100, 101], "low": [99, 98], "close": [100, 100]})
        # A clean 8/8 bullish setup EXCEPT atr is NaN -- must not open a
        # simulated trade at all (not even a degenerate zero-risk one).
        nan_atr_ind = {
            "close": 110.0, "ema9": 105.0, "ema21": 99.0,
            "ema9_slope": 1.0, "ema21_slope": 1.0, "sar": 95.0,
            "macd": 2.0, "macd_signal": 1.0, "macd_hist": 1.0, "macd_hist_prev": 0.5,
            "rsi": 60.0, "relative_volume": 2.0, "atr": float("nan"),
        }
        with patch.object(strat, "indicator_dict_at", return_value=nan_atr_ind), \
             patch.object(strat, "classify_regime", return_value="trending"):
            r_multiples = strat._simulate_directional_r_multiples(df, "up")
        self.assertEqual(r_multiples, [])

    def test_evaluate_index_direction_trade_no_trades_on_nan_atr_instead_of_crashing(self):
        from unittest.mock import patch

        from common.constants import SignalStatus, SignalType

        from . import index_direction_strategy as strat

        fake_ind = {
            "close": 24500.0, "atr": float("nan"), "rsi": 40, "adx": 30,
            "bb_width": 0.02, "relative_volume": 1.5, "ema9": 0, "ema21": 0,
            "ema9_slope": 0, "ema21_slope": 0, "macd_hist_prev": 0, "macd_hist": 0,
            "macd": 0, "macd_signal": 0, "sar": 0,
        }
        direction_result = {
            "direction": "down", "option_side": "PE", "score": 0.9,
            "regime": "trending", "ind": fake_ind, "detail": "forced for test",
        }
        success = {
            "available": True, "trade_count": 10, "win_rate": 0.6,
            "expectancy_r": 0.3, "profit_factor": 1.8, "profitable": True,
            "detail": "forced profitable for test",
        }
        sentiment = {
            "sentiment_score": 0.0, "has_contradictory_headline": False,
            "has_strongly_positive_headline": False, "headline_count": 0, "confidence": 0.0,
        }

        with patch.object(strat, "determine_index_direction", return_value=direction_result), \
             patch.object(strat, "success_rate_for_side", return_value=success), \
             patch.object(strat, "aggregate_sentiment", return_value=sentiment):
            signal = strat.evaluate_index_direction_trade("NIFTY", "5m")

        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertEqual(signal.signal_type, SignalType.NO_TRADE)
        self.assertIn("ATR is invalid", signal.reason)
        self.assertEqual(signal.entry_price, signal.stop_loss)


class SyncWatchlistOptionContractsTests(TestCase):
    """apps.options.tasks.sync_watchlist_option_contracts -- BROKER_MODE guard only (no real broker call in tests)."""

    def test_skips_outside_live_broker_mode(self):
        from django.test import override_settings

        from .tasks import sync_watchlist_option_contracts

        with override_settings(BROKER_MODE="paper"):
            result = sync_watchlist_option_contracts()
        self.assertTrue(result.get("skipped"))
        self.assertIn("BROKER_MODE", result.get("reason", ""))


def _fake_contract(strike, option_type, expiry, token_suffix=""):
    """Same shape apps.options.instrument_master.options_for_expiry/broker_client.OptionChainClient.fetch_contract_list really return."""
    return {
        "strike": float(strike), "option_type": option_type,
        "symbol_token": f"tok_{expiry.isoformat()}_{strike}_{option_type}{token_suffix}",
        "tradingsymbol": f"TESTIDX{expiry.strftime('%d%b%y').upper()}{int(strike)}{option_type}",
        "lot_size": 75,
    }


class _FakeOptionChainClient:
    """
    Mocked apps.options.broker_client.OptionChainClient -- unit tests
    must not call the live Angel One API (per this platform's own
    testing rule), so every ContractSyncTests scenario configures this
    fake's per-expiry contract lists / raised exceptions directly rather
    than hitting the network.
    """

    def __init__(self, contracts_by_expiry=None, raise_on_expiry=None):
        self.contracts_by_expiry = contracts_by_expiry or {}
        self.raise_on_expiry = raise_on_expiry or {}
        self.calls = []

    def fetch_contract_list(self, underlying, expiry):
        self.calls.append((underlying, expiry))
        if expiry in self.raise_on_expiry:
            raise self.raise_on_expiry[expiry]
        return self.contracts_by_expiry.get(expiry, [])


class ContractSyncTests(TestCase):
    """
    apps.options.contract_sync.sync_underlying_contracts -- the one
    idempotent sync routine used by both `sync_option_contracts` and
    apps.options.tasks' Celery jobs. Every scenario here mocks
    apps.options.broker_client.get_option_chain_client and
    apps.options.instrument_master.list_expiries/get_instrument_master
    directly (no real Angel One or network call).
    """

    UNDERLYING = "TESTIDX"

    def setUp(self):
        # sync_underlying_contracts locks per-underlying via Redis
        # (apps.options.sync_lock) -- real Redis is available in this
        # dev/test environment (same REDIS_URL Celery's own broker
        # already requires), so these tests exercise the real lock
        # rather than mocking it away, proving the whole path actually
        # works end-to-end. A unique underlying name per test class
        # keeps lock keys from colliding across parallel test runs.
        pass

    def _patch_client(self, client):
        return patch("apps.options.broker_client.get_option_chain_client", return_value=client)

    def _patch_expiries(self, expiries):
        return patch("apps.options.instrument_master.list_expiries", return_value=expiries)

    # 10. Empty database recovery.
    def test_empty_database_recovery_creates_contracts(self):
        from .contract_sync import sync_underlying_contracts

        expiry = date.today() + timedelta(days=3)
        client = _FakeOptionChainClient({
            expiry: [_fake_contract(24000, "CE", expiry), _fake_contract(24000, "PE", expiry)],
        })
        self.assertEqual(OptionContract.objects.count(), 0)
        with self._patch_client(client), self._patch_expiries([expiry]):
            result = sync_underlying_contracts(self.UNDERLYING)

        self.assertTrue(result.ok)
        self.assertEqual(result.inserted, 2)
        self.assertEqual(result.updated, 0)
        self.assertEqual(OptionContract.objects.filter(underlying=self.UNDERLYING).count(), 2)
        self.assertTrue(OptionContract.objects.get(underlying=self.UNDERLYING, option_type="CE").is_active)

    # 11. Stale cached instrument master -- force_refresh busts it.
    def test_force_refresh_busts_instrument_master_cache(self):
        from .contract_sync import sync_underlying_contracts

        expiry = date.today() + timedelta(days=3)
        client = _FakeOptionChainClient({expiry: [_fake_contract(24000, "CE", expiry)]})
        with self._patch_client(client), self._patch_expiries([expiry]) as mocked_list, \
             patch("apps.options.instrument_master.get_instrument_master") as mocked_master:
            sync_underlying_contracts(self.UNDERLYING, force_refresh=True)
            mocked_master.assert_called_once_with(force_refresh=True)

    # 12. Network timeout.
    def test_network_timeout_is_caught_and_recorded_not_raised(self):
        from .contract_sync import sync_underlying_contracts
        from .instrument_master import InstrumentMasterError
        from .models import OptionSyncStatus

        with patch("apps.options.instrument_master.list_expiries", side_effect=InstrumentMasterError("timed out after 4 attempts")):
            result = sync_underlying_contracts(self.UNDERLYING)

        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)
        self.assertEqual(OptionContract.objects.filter(underlying=self.UNDERLYING).count(), 0)
        status_row = OptionSyncStatus.objects.get(underlying=self.UNDERLYING)
        self.assertIn("timed out", status_row.last_error)
        self.assertIsNone(status_row.last_successful_sync)

    # 13. Invalid or partial master response (per-contract malformed records).
    def test_malformed_contract_records_are_skipped_not_fatal(self):
        from .contract_sync import sync_underlying_contracts

        expiry = date.today() + timedelta(days=3)
        good = _fake_contract(24000, "CE", expiry)
        malformed = {"strike": 24100.0, "option_type": "PE"}  # missing symbol_token/tradingsymbol/lot_size
        client = _FakeOptionChainClient({expiry: [good, malformed]})
        with self._patch_client(client), self._patch_expiries([expiry]):
            result = sync_underlying_contracts(self.UNDERLYING)

        self.assertTrue(result.ok)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.invalid_skipped, 1)
        self.assertEqual(OptionContract.objects.filter(underlying=self.UNDERLYING).count(), 1)

    # 14. Transaction rollback after failure.
    def test_failure_on_second_expiry_rolls_back_the_whole_sync(self):
        from .contract_sync import sync_underlying_contracts

        expiry_1 = date.today() + timedelta(days=3)
        expiry_2 = date.today() + timedelta(days=10)
        client = _FakeOptionChainClient(
            contracts_by_expiry={expiry_1: [_fake_contract(24000, "CE", expiry_1)]},
            raise_on_expiry={expiry_2: RuntimeError("simulated broker failure mid-sync")},
        )
        with self._patch_client(client), self._patch_expiries([expiry_1, expiry_2]):
            result = sync_underlying_contracts(self.UNDERLYING)

        self.assertFalse(result.ok)
        # Nothing committed -- not even expiry_1's contract, which
        # succeeded BEFORE the failure -- because the whole underlying's
        # sync is one transaction.
        self.assertEqual(OptionContract.objects.filter(underlying=self.UNDERLYING).count(), 0)

    # 15. Duplicate synchronization runs.
    def test_duplicate_sync_runs_are_idempotent(self):
        from .contract_sync import sync_underlying_contracts

        expiry = date.today() + timedelta(days=3)
        client = _FakeOptionChainClient({expiry: [_fake_contract(24000, "CE", expiry), _fake_contract(24000, "PE", expiry)]})
        with self._patch_client(client), self._patch_expiries([expiry]):
            first = sync_underlying_contracts(self.UNDERLYING)
            second = sync_underlying_contracts(self.UNDERLYING)

        self.assertEqual(first.inserted, 2)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.updated, 2)
        self.assertEqual(OptionContract.objects.filter(underlying=self.UNDERLYING).count(), 2)

    def test_dry_run_writes_nothing(self):
        from .contract_sync import sync_underlying_contracts

        expiry = date.today() + timedelta(days=3)
        client = _FakeOptionChainClient({expiry: [_fake_contract(24000, "CE", expiry)]})
        with self._patch_client(client), self._patch_expiries([expiry]):
            result = sync_underlying_contracts(self.UNDERLYING, dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertEqual(result.inserted, 1)  # reported...
        self.assertEqual(OptionContract.objects.filter(underlying=self.UNDERLYING).count(), 0)  # ...but never committed
        from .models import OptionSyncStatus

        self.assertFalse(OptionSyncStatus.objects.filter(underlying=self.UNDERLYING).exists())

    def test_deactivates_contracts_that_rolled_past_cutoff(self):
        """
        A contract from a PREVIOUSLY-synced expiry that has since rolled
        past the cutoff must flip to is_active=False even if the
        current sync's expiry window no longer includes it.
        """
        from .contract_sync import sync_underlying_contracts

        rolled_over = OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=date.today() - timedelta(days=1),
            strike=100, option_type="CE", symbol_token="tok_old", is_active=True,
        )
        expiry = date.today() + timedelta(days=3)
        client = _FakeOptionChainClient({expiry: [_fake_contract(24000, "CE", expiry)]})
        with self._patch_client(client), self._patch_expiries([expiry]):
            result = sync_underlying_contracts(self.UNDERLYING)

        rolled_over.refresh_from_db()
        self.assertFalse(rolled_over.is_active)
        self.assertGreaterEqual(result.deactivated, 1)


class InstrumentMasterValidationTests(TestCase):
    """
    apps.options.instrument_master's download validation/retry/backoff --
    mocked HTTP responses only, no real network call. `time.sleep` is
    patched to a no-op so retry-backoff tests run instantly rather than
    actually waiting several seconds.
    """

    def test_empty_list_response_raises_and_is_not_cached(self):
        from . import instrument_master
        from .instrument_master import InstrumentMasterError, get_instrument_master

        instrument_master._cache["data"] = None
        instrument_master._cache["fetched_at"] = 0.0
        fake_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: []})()
        with patch("apps.options.instrument_master.requests.get", return_value=fake_response), \
             patch("apps.options.instrument_master.time.sleep"):
            with self.assertRaises(InstrumentMasterError):
                get_instrument_master(force_refresh=True)
        self.assertIsNone(instrument_master._cache["data"])

    def test_malformed_rows_missing_required_keys_raises(self):
        from . import instrument_master
        from .instrument_master import InstrumentMasterError, get_instrument_master

        instrument_master._cache["data"] = None
        instrument_master._cache["fetched_at"] = 0.0
        bad_rows = [{"token": "1", "symbol": "X", "instrumenttype": "OPTIDX"}] * 5  # missing expiry/exch_seg/strike/name
        fake_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: bad_rows})()
        with patch("apps.options.instrument_master.requests.get", return_value=fake_response), \
             patch("apps.options.instrument_master.time.sleep"):
            with self.assertRaises(InstrumentMasterError):
                get_instrument_master(force_refresh=True)

    def test_retries_then_succeeds(self):
        import requests

        from . import instrument_master
        from .instrument_master import get_instrument_master

        instrument_master._cache["data"] = None
        instrument_master._cache["fetched_at"] = 0.0
        good_row = {
            "token": "1", "symbol": "TESTIDX21AUG26CE", "name": "TESTIDX", "expiry": "21AUG2026",
            "instrumenttype": "OPTIDX", "exch_seg": "NFO", "strike": "2400000.000000",
        }
        good_response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: [good_row] * 5})()
        call_count = {"n": 0}

        def flaky_get(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise requests.exceptions.Timeout("simulated timeout")
            return good_response

        with patch("apps.options.instrument_master.requests.get", side_effect=flaky_get), \
             patch("apps.options.instrument_master.time.sleep") as mocked_sleep:
            data = get_instrument_master(force_refresh=True)

        self.assertEqual(len(data), 5)
        self.assertEqual(call_count["n"], 3)
        self.assertEqual(mocked_sleep.call_count, 2)  # backed off before attempts 2 and 3

    def test_exhausted_retries_serves_stale_cache_if_one_exists(self):
        from . import instrument_master
        from .instrument_master import get_instrument_master

        good_row = {
            "token": "1", "symbol": "TESTIDX21AUG26CE", "name": "TESTIDX", "expiry": "21AUG2026",
            "instrumenttype": "OPTIDX", "exch_seg": "NFO", "strike": "2400000.000000",
        }
        instrument_master._cache["data"] = [good_row] * 5
        instrument_master._cache["fetched_at"] = 0.0  # stale (older than _CACHE_TTL_SECONDS)

        import requests

        with patch("apps.options.instrument_master.requests.get", side_effect=requests.exceptions.ConnectionError("down")), \
             patch("apps.options.instrument_master.time.sleep"):
            data = get_instrument_master()  # not force_refresh -- stale cache triggers a refresh attempt that fails

        self.assertEqual(len(data), 5)  # fell back to the stale-but-real cached copy, not an exception


class SyncLockTests(TestCase):
    """
    apps.options.sync_lock -- a real Redis lock (this dev/test
    environment already has Redis up for Celery's own broker, same
    REDIS_URL). Proves two concurrent holders of the same key can never
    both acquire, and that release lets a subsequent acquire succeed.
    """

    def setUp(self):
        from .sync_lock import release_lock

        self.key = "options:sync_lock_test:16"
        self._release = release_lock
        # Defensive cleanup in case a previous failed run left this key set.
        try:
            import redis

            redis.Redis.from_url("redis://127.0.0.1:6379/0").delete(self.key)
        except Exception:
            pass

    def tearDown(self):
        try:
            import redis

            redis.Redis.from_url("redis://127.0.0.1:6379/0").delete(self.key)
        except Exception:
            pass

    def test_second_concurrent_acquire_fails_while_first_holds_it(self):
        from .sync_lock import acquire_lock

        token_a = acquire_lock(self.key, timeout=30)
        token_b = acquire_lock(self.key, timeout=30)
        self.assertIsNotNone(token_a)
        self.assertIsNone(token_b)
        self._release(self.key, token_a)

    def test_release_then_reacquire_succeeds(self):
        from .sync_lock import acquire_lock

        token_a = acquire_lock(self.key, timeout=30)
        self._release(self.key, token_a)
        token_c = acquire_lock(self.key, timeout=30)
        self.assertIsNotNone(token_c)
        self._release(self.key, token_c)

    def test_release_with_wrong_token_does_not_release_someone_elses_lock(self):
        from .sync_lock import acquire_lock

        token_a = acquire_lock(self.key, timeout=30)
        self._release(self.key, "not-the-real-token")  # must be a safe no-op
        token_b = acquire_lock(self.key, timeout=30)
        self.assertIsNone(token_b)  # token_a's lock is still held -- wrong-token release didn't clear it
        self._release(self.key, token_a)

    def test_sync_underlying_contracts_skips_when_already_locked(self):
        """
        End-to-end: a manual sync call for an underlying that's already
        locked (simulating a concurrent Celery task/another worker/a
        second manual command run) must skip cleanly, not race.
        """
        from .contract_sync import SYNC_LOCK_KEY_PREFIX, sync_underlying_contracts
        from .sync_lock import acquire_lock, release_lock

        underlying = "LOCKTESTIDX"
        lock_key = f"{SYNC_LOCK_KEY_PREFIX}:{underlying}"
        token = acquire_lock(lock_key, timeout=30)
        try:
            result = sync_underlying_contracts(underlying)
            self.assertFalse(result.ok)
            self.assertEqual(result.skipped_reason, "sync_already_in_progress")
        finally:
            release_lock(lock_key, token)


class RolloverSelfHealTests(TestCase):
    """
    apps.options.tasks.options_sync_health_check / rollover_expiries --
    the self-healing path for "Celery Beat was stopped across the
    scheduled rollover." options_sync_health_check itself must never
    call Angel One (rollover_required is DB-only); rollover_expiries
    (the thing it triggers) is what actually resyncs, mocked here.
    """

    def test_health_check_triggers_resync_when_no_eligible_expiry(self):
        from .tasks import options_sync_health_check

        with override_settings(OPTIONS_PIPELINE_UNDERLYINGS=["HEALTHCHECKIDX"]), \
             patch("apps.options.tasks.rollover_expiries.delay") as mocked_delay:
            result = options_sync_health_check()

        self.assertIn("HEALTHCHECKIDX", result["unhealthy"])
        self.assertTrue(result["rollover_triggered"])
        mocked_delay.assert_called_once()

    def test_health_check_does_not_trigger_when_healthy(self):
        from .tasks import options_sync_health_check

        underlying = "HEALTHYIDX"
        for offset in (3, 10, 17, 24, 31, 38):
            OptionContract.objects.create(
                underlying=underlying, expiry=date.today() + timedelta(days=offset),
                strike=100, option_type="CE", symbol_token=f"tok_health_{offset}",
            )
        with override_settings(OPTIONS_PIPELINE_UNDERLYINGS=[underlying], OPTIONS_EXPIRY_SYNC_COUNT=4), \
             patch("apps.options.tasks.rollover_expiries.delay") as mocked_delay:
            result = options_sync_health_check()

        self.assertEqual(result["unhealthy"], [])
        self.assertFalse(result["rollover_triggered"])
        mocked_delay.assert_not_called()

    def test_rollover_expiries_resyncs_only_underlyings_that_need_it(self):
        from .tasks import rollover_expiries

        needs_rollover = "NEEDSROLLOVERIDX"
        already_fine = "ALREADYFINEIDX"
        for offset in (3, 10, 17, 24, 31, 38):
            OptionContract.objects.create(
                underlying=already_fine, expiry=date.today() + timedelta(days=offset),
                strike=100, option_type="CE", symbol_token=f"tok_fine_{offset}",
            )
        expiry = date.today() + timedelta(days=3)
        client = _FakeOptionChainClient({expiry: [_fake_contract(24000, "CE", expiry)]})

        with override_settings(
            OPTIONS_PIPELINE_UNDERLYINGS=[needs_rollover, already_fine], OPTIONS_EXPIRY_SYNC_COUNT=4, BROKER_MODE="live",
        ), patch("apps.options.broker_client.get_option_chain_client", return_value=client), \
           patch("apps.options.instrument_master.list_expiries", return_value=[expiry]):
            results = rollover_expiries()

        self.assertTrue(results[already_fine]["skipped"])
        self.assertEqual(results[already_fine]["reason"], "rollover_not_required")
        self.assertTrue(results[needs_rollover]["ok"])
        self.assertEqual(OptionContract.objects.filter(underlying=needs_rollover).count(), 1)


class OptionExpiryStatusViewTests(APITestCase):
    """GET /api/options/expiry-status/ -- apps.options.views.OptionExpiryStatusView."""

    UNDERLYING = "STATUSTESTIDX"

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Trader")[0])
        self.client.force_authenticate(self.user)

    def test_returns_current_and_next_expiry_chronologically(self):
        # 6 expiries -- exactly settings.OPTIONS_EXPIRY_SYNC_COUNT's
        # default, so rollover_required (< sync count) is deterministically
        # false here rather than incidentally true/false depending on
        # how many offsets happen to be listed.
        for offset in (3, 10, 17, 24, 31, 38):
            OptionContract.objects.create(
                underlying=self.UNDERLYING, expiry=date.today() + timedelta(days=offset),
                strike=100, option_type="CE", symbol_token=f"tok_status_{offset}",
            )
        response = self.client.get("/api/options/expiry-status/", {"underlying": self.UNDERLYING})
        self.assertEqual(response.status_code, 200)
        expiries = response.data["available_expiries"]
        self.assertEqual(expiries, sorted(expiries))
        self.assertEqual(response.data["current_expiry"], expiries[0])
        self.assertEqual(response.data["next_expiry"], expiries[1])
        self.assertFalse(response.data["rollover_required"])

    def test_503_when_only_expired_contract_exists(self):
        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=date.today() - timedelta(days=1),
            strike=100, option_type="CE", symbol_token="tok_status_expired",
        )
        response = self.client.get("/api/options/expiry-status/", {"underlying": self.UNDERLYING})
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(response.data["current_expiry"])
        self.assertTrue(response.data["rollover_required"])

    def test_iso_format_and_last_successful_sync_surfaced(self):
        from .models import OptionSyncStatus

        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=date.today() + timedelta(days=3),
            strike=100, option_type="CE", symbol_token="tok_status_iso",
        )
        synced_at = timezone.now()
        OptionSyncStatus.objects.create(underlying=self.UNDERLYING, last_successful_sync=synced_at)

        response = self.client.get("/api/options/expiry-status/", {"underlying": self.UNDERLYING})
        self.assertRegex(response.data["current_expiry"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(
            date.fromisoformat(response.data["last_successful_sync"][:10]), synced_at.date(),
        )


class OptionChainViewFallbackTests(APITestCase):
    """
    GET /api/options/chain/ -- apps.options.views.OptionChainView's new
    expired-expiry fallback (never silently serves a stale chain).
    """

    UNDERLYING = "CHAINFALLBACKIDX"

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Trader")[0])
        self.client.force_authenticate(self.user)
        self.expired = date.today() - timedelta(days=1)
        self.current = date.today() + timedelta(days=3)
        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=self.expired, strike=100,
            option_type="CE", symbol_token="tok_chain_expired",
        )
        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=self.current, strike=100,
            option_type="CE", symbol_token="tok_chain_current",
        )

    # 18. API request containing an expired expiry.
    def test_expired_expiry_request_falls_back_to_current(self):
        response = self.client.get(
            "/api/options/chain/", {"underlying": self.UNDERLYING, "expiry": self.expired.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["expiry"], self.current.isoformat())
        self.assertTrue(response.data["substituted_expiry"])

    def test_still_valid_expiry_request_is_honored_unchanged(self):
        response = self.client.get(
            "/api/options/chain/", {"underlying": self.UNDERLYING, "expiry": self.current.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["expiry"], self.current.isoformat())
        self.assertFalse(response.data["substituted_expiry"])

    def test_no_valid_expiry_at_all_returns_503(self):
        OptionContract.objects.filter(underlying=self.UNDERLYING, expiry=self.current).delete()
        response = self.client.get(
            "/api/options/chain/", {"underlying": self.UNDERLYING, "expiry": self.expired.isoformat()},
        )
        self.assertEqual(response.status_code, 503)


class SyncOptionContractsCommandTests(TestCase):
    """python manage.py sync_option_contracts -- expanded flags, dry-run, exit codes."""

    def test_dry_run_all_reports_but_writes_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        expiry = date.today() + timedelta(days=3)
        client = _FakeOptionChainClient({expiry: [_fake_contract(24000, "CE", expiry)]})
        out = StringIO()
        with override_settings(OPTIONS_PIPELINE_UNDERLYINGS=["CMDTESTIDX"]), \
             patch("apps.options.broker_client.get_option_chain_client", return_value=client), \
             patch("apps.options.instrument_master.list_expiries", return_value=[expiry]):
            call_command("sync_option_contracts", "--all", "--dry-run", stdout=out)

        self.assertIn("DRY RUN", out.getvalue())
        self.assertEqual(OptionContract.objects.filter(underlying="CMDTESTIDX").count(), 0)

    def test_underlying_flag_syncs_just_that_one(self):
        from io import StringIO

        from django.core.management import call_command

        expiry = date.today() + timedelta(days=3)
        client = _FakeOptionChainClient({expiry: [_fake_contract(24000, "CE", expiry)]})
        out = StringIO()
        with patch("apps.options.broker_client.get_option_chain_client", return_value=client), \
             patch("apps.options.instrument_master.list_expiries", return_value=[expiry]):
            call_command("sync_option_contracts", "--underlying", "CMDONEIDX", stdout=out)

        self.assertEqual(OptionContract.objects.filter(underlying="CMDONEIDX").count(), 1)
        self.assertIn("inserted=1", out.getvalue())

    def test_failure_exits_nonzero(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        from .instrument_master import InstrumentMasterError

        with patch("apps.options.instrument_master.list_expiries", side_effect=InstrumentMasterError("down")):
            with self.assertRaises(SystemExit) as ctx:
                call_command("sync_option_contracts", "--underlying", "CMDFAILIDX")
        self.assertNotEqual(ctx.exception.code, 0)

    def test_no_arguments_raises_command_error(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("sync_option_contracts")


class DataQualityValidatorTests(TestCase):
    """
    apps.options.data_quality -- pure-function per-field validators (no
    DB). Each one is a plain (ok, reason) check, mirroring apps.risk.
    engine's own check style.
    """

    def test_validate_ltp(self):
        from .data_quality import validate_ltp

        self.assertTrue(validate_ltp(112.20)[0])
        self.assertFalse(validate_ltp(None)[0])
        self.assertFalse(validate_ltp(0)[0])
        self.assertFalse(validate_ltp(-5)[0])

    def test_validate_bid_ask(self):
        from .data_quality import validate_bid_ask

        self.assertTrue(validate_bid_ask(109.0, 111.0)[0])
        self.assertFalse(validate_bid_ask(None, 111.0)[0])
        self.assertFalse(validate_bid_ask(111.0, 109.0)[0])  # crossed
        self.assertFalse(validate_bid_ask(0, 0)[0])

    def test_validate_iv_rejects_outside_solver_bounds(self):
        from .data_quality import validate_iv

        self.assertTrue(validate_iv(18.5)[0])
        self.assertFalse(validate_iv(None)[0])
        self.assertFalse(validate_iv(0)[0])
        self.assertFalse(validate_iv(600)[0])  # beyond greeks.py's own solver bound of 500%

    def test_validate_greeks_rejects_out_of_range_delta(self):
        from .data_quality import validate_greeks

        self.assertTrue(validate_greeks({"delta": 0.55, "gamma": 0.001, "vega": 5.0})[0])
        self.assertFalse(validate_greeks(None)[0])
        self.assertFalse(validate_greeks({"delta": 1.5})[0])
        self.assertFalse(validate_greeks({"delta": 0.5, "gamma": -0.001})[0])

    def test_detect_stale_quotes(self):
        from datetime import timedelta

        from .data_quality import detect_stale_quotes

        now = timezone.now()
        is_stale, _ = detect_stale_quotes(now - timedelta(minutes=30), now=now, threshold_minutes=15)
        self.assertTrue(is_stale)
        is_stale, _ = detect_stale_quotes(now - timedelta(minutes=2), now=now, threshold_minutes=15)
        self.assertFalse(is_stale)
        self.assertTrue(detect_stale_quotes(None)[0])

    def test_detect_bad_ticks_flags_ltp_far_outside_bid_ask(self):
        from .data_quality import detect_bad_ticks

        self.assertFalse(detect_bad_ticks(110.0, 109.0, 111.0)[0])
        self.assertTrue(detect_bad_ticks(200.0, 109.0, 111.0)[0])  # LTP way outside the quoted spread
        self.assertTrue(detect_bad_ticks(110.0, 111.0, 109.0)[0])  # crossed

    def test_detect_wide_spread_contracts(self):
        from .data_quality import detect_wide_spread_contracts

        self.assertFalse(detect_wide_spread_contracts(109.0, 111.0, max_spread_pct=10.0)[0])
        self.assertTrue(detect_wide_spread_contracts(90.0, 130.0, max_spread_pct=10.0)[0])


class ValidateOptionChainSnapshotTests(TestCase):
    """apps.options.data_quality.validate_option_chain_snapshot -- the chain-wide gate wired into evaluate_index_direction_trade."""

    def setUp(self):
        self.expiry = date.today() + timedelta(days=7)
        self.contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24400,
            option_type="CE", symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE", lot_size=25,
        )

    def test_valid_with_a_fresh_sane_snapshot(self):
        from .data_quality import validate_option_chain_snapshot

        OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=timezone.now(),
            ltp=Decimal("112.20"), open_interest=5000, change_in_oi=0, volume=1000,
        )
        report = validate_option_chain_snapshot("NIFTY", self.expiry)
        self.assertTrue(report.valid)
        self.assertEqual(report.status, "DATA_VALID")

    def test_invalid_when_every_snapshot_is_stale(self):
        from datetime import timedelta as td

        from .data_quality import validate_option_chain_snapshot

        OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=timezone.now() - td(minutes=60),
            ltp=Decimal("112.20"), open_interest=5000, change_in_oi=0, volume=1000,
        )
        report = validate_option_chain_snapshot("NIFTY", self.expiry)
        self.assertFalse(report.valid)
        self.assertEqual(report.status, "DATA_INVALID")

    def test_invalid_when_no_contracts_synced(self):
        from .data_quality import validate_option_chain_snapshot

        report = validate_option_chain_snapshot("BANKNIFTY", self.expiry)
        self.assertFalse(report.valid)

    def test_invalid_for_an_expired_expiry(self):
        from .data_quality import validate_option_chain_snapshot

        report = validate_option_chain_snapshot("NIFTY", date.today() - timedelta(days=1))
        self.assertFalse(report.valid)


class VolatilitySurfaceTests(TestCase):
    """apps.options.volatility_surface -- IV percentile (pure) + skew/term-structure (DB-backed, real Greeks)."""

    def test_calculate_iv_percentile(self):
        from .volatility_surface import calculate_iv_percentile

        self.assertEqual(calculate_iv_percentile(15.0, [10.0, 12.0, 14.0, 16.0, 18.0]), 60.0)
        self.assertIsNone(calculate_iv_percentile(None, [10.0, 12.0]))
        self.assertIsNone(calculate_iv_percentile(15.0, []))

    def test_detect_contango_or_backwardation_pure(self):
        from .volatility_surface import detect_contango_or_backwardation

        contango = [{"days_to_expiry": 7, "atm_iv": 14.0}, {"days_to_expiry": 30, "atm_iv": 18.0}]
        self.assertEqual(detect_contango_or_backwardation(contango), "contango")

        backwardation = [{"days_to_expiry": 7, "atm_iv": 22.0}, {"days_to_expiry": 30, "atm_iv": 16.0}]
        self.assertEqual(detect_contango_or_backwardation(backwardation), "backwardation")

        self.assertEqual(detect_contango_or_backwardation([{"days_to_expiry": 7, "atm_iv": 14.0}]), "insufficient_data")

    def test_calculate_atm_iv_and_skew_with_real_chain_fixture(self):
        from apps.market_data.models import HistoricalData

        from .volatility_surface import calculate_25_delta_skew, calculate_atm_iv, calculate_call_skew

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        # ATM CE (~0.59 delta, matches SuggestBestStrikeTests' known fixture)
        atm_call = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=atm_call, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)
        # Deep OTM CE, priced cheaply -> low delta, a plausible ~0.25-delta candidate
        otm_call = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=25200, option_type="CE",
            symbol_token="tok_ce_25200", tradingsymbol="NIFTY25200CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=otm_call, timestamp=timezone.now(), ltp=Decimal("40.0"), open_interest=3000, change_in_oi=0, volume=800)
        atm_put = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="PE",
            symbol_token="tok_pe_24400", tradingsymbol="NIFTY24400PE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=atm_put, timestamp=timezone.now(), ltp=Decimal("220.0"), open_interest=4000, change_in_oi=0, volume=900)
        otm_put = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=23800, option_type="PE",
            symbol_token="tok_pe_23800", tradingsymbol="NIFTY23800PE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=otm_put, timestamp=timezone.now(), ltp=Decimal("35.0"), open_interest=2500, change_in_oi=0, volume=700)

        atm_iv = calculate_atm_iv("NIFTY", expiry, 24500)
        self.assertIsNotNone(atm_iv["call_iv"])
        self.assertIsNotNone(atm_iv["put_iv"])
        self.assertIsNotNone(atm_iv["average_iv"])

        call_skew = calculate_call_skew("NIFTY", expiry, 24500)
        self.assertIsNotNone(call_skew)  # a real number, not asserting a specific sign/magnitude on synthetic data

        skew_25d = calculate_25_delta_skew("NIFTY", expiry, 24500)
        self.assertIsNotNone(skew_25d)

    def test_calculate_atm_iv_returns_none_side_when_unavailable(self):
        from .volatility_surface import calculate_atm_iv

        expiry = date.today() + timedelta(days=7)
        result = calculate_atm_iv("NIFTY", expiry, 24500)
        self.assertIsNone(result["call_iv"])
        self.assertIsNone(result["put_iv"])
        self.assertIsNone(result["average_iv"])


class ExposureTests(TestCase):
    """apps.options.exposure -- gamma/vanna/charm exposure proxies, all explicitly labeled MODELED."""

    def setUp(self):
        from apps.market_data.models import HistoricalData

        self.expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        call = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24400, option_type="CE",
            symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=call, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)
        put = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24400, option_type="PE",
            symbol_token="tok_pe_24400", tradingsymbol="NIFTY24400PE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=put, timestamp=timezone.now(), ltp=Decimal("220.0"), open_interest=4000, change_in_oi=0, volume=900)

    def test_net_gamma_exposure_is_modeled_and_uses_both_sides(self):
        from .exposure import calculate_net_gamma_exposure

        result = calculate_net_gamma_exposure("NIFTY", self.expiry, 24500)
        self.assertEqual(result["label"], "MODELED")
        self.assertIn("assumption", result)
        self.assertEqual(result["contracts_used"], 2)
        self.assertIsNotNone(result["net_gamma_exposure"])
        self.assertIsNotNone(result["call_gamma_exposure"])
        self.assertIsNotNone(result["put_gamma_exposure"])

    def test_net_gamma_exposure_none_when_no_contracts(self):
        from .exposure import calculate_net_gamma_exposure

        result = calculate_net_gamma_exposure("BANKNIFTY", self.expiry, 55000)
        self.assertIsNone(result["net_gamma_exposure"])
        self.assertEqual(result["contracts_used"], 0)

    def test_vanna_exposure_is_modeled(self):
        from .exposure import calculate_vanna_exposure

        result = calculate_vanna_exposure("NIFTY", self.expiry, 24500)
        self.assertEqual(result["label"], "MODELED")
        self.assertEqual(result["contracts_used"], 2)
        self.assertIsNotNone(result["net_exposure"])

    def test_charm_exposure_is_modeled(self):
        from .exposure import calculate_charm_exposure

        result = calculate_charm_exposure("NIFTY", self.expiry, 24500)
        self.assertEqual(result["label"], "MODELED")
        self.assertEqual(result["contracts_used"], 2)
        self.assertIsNotNone(result["net_exposure"])


class ExpectedMoveTests(TestCase):
    """apps.options.expected_move -- hand-computed against spot=24500, iv=15%, dte=7 (math.sqrt-verified, not eyeballed)."""

    def test_calculate_expected_move_matches_hand_computed_value(self):
        from .expected_move import calculate_expected_move

        result = calculate_expected_move(24500, 15, 7)
        self.assertAlmostEqual(result["expected_move"], 508.93, places=2)
        self.assertAlmostEqual(result["upper_range"], 25008.93, places=2)
        self.assertAlmostEqual(result["lower_range"], 23991.07, places=2)
        self.assertEqual(result["days_to_expiry"], 7)

    def test_calculate_expected_move_none_on_missing_inputs(self):
        from .expected_move import calculate_expected_move

        self.assertIsNone(calculate_expected_move(None, 15, 7)["expected_move"])
        self.assertIsNone(calculate_expected_move(24500, None, 7)["expected_move"])
        self.assertIsNone(calculate_expected_move(24500, 15, None)["expected_move"])

    def test_classify_price_vs_expected_range(self):
        from .expected_move import classify_price_vs_expected_range

        # Range [23991.07, 25008.93], width ~1017.86, near_threshold=10%
        self.assertEqual(classify_price_vs_expected_range(24500, 25008.93, 23991.07), "inside_range")
        self.assertEqual(classify_price_vs_expected_range(25100, 25008.93, 23991.07), "outside_upper_range")
        self.assertEqual(classify_price_vs_expected_range(23900, 25008.93, 23991.07), "outside_lower_range")
        self.assertEqual(classify_price_vs_expected_range(24990, 25008.93, 23991.07), "near_upper_range")
        self.assertEqual(classify_price_vs_expected_range(None, 25008.93, 23991.07), "unavailable")

    def test_calculate_expected_move_for_contract_with_real_chain_fixture(self):
        from apps.market_data.models import HistoricalData

        from .expected_move import calculate_expected_move_for_contract

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)

        result = calculate_expected_move_for_contract("NIFTY", expiry, 24500)
        self.assertIsNotNone(result["expected_move"])
        self.assertIsNotNone(result["atm_iv"])
        self.assertGreater(result["upper_range"], 24500)
        self.assertLess(result["lower_range"], 24500)


class SupportResistanceTests(TestCase):
    """apps.options.support_resistance -- confluence zones from OI + VWAP + prior-day + expected-move, no single source dominates."""

    def setUp(self):
        from apps.market_data.models import HistoricalData

        self.expiry = date.today() + timedelta(days=7)
        self.today = timezone.localdate()

        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        # Prior day daily candle
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="1d", timestamp=timezone.now() - timedelta(days=1),
            open=24300, high=24450, low=24250, close=24400, volume=500000, source="test",
        )
        call = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24600, option_type="CE",
            symbol_token="tok_ce_24600", tradingsymbol="NIFTY24600CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=call, timestamp=timezone.now(), ltp=Decimal("150.0"), open_interest=9000, change_in_oi=0, volume=2000)
        put = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24300, option_type="PE",
            symbol_token="tok_pe_24300", tradingsymbol="NIFTY24300PE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=put, timestamp=timezone.now(), ltp=Decimal("120.0"), open_interest=8500, change_in_oi=0, volume=1800)

    def test_dynamic_support_includes_oi_put_wall_below_spot(self):
        from .support_resistance import calculate_dynamic_support

        zones = calculate_dynamic_support("NIFTY", self.expiry, 24500)
        self.assertTrue(any("oi_put_wall" in z["sources"] for z in zones))
        for z in zones:
            self.assertLess(z["level"], 24500)

    def test_dynamic_resistance_includes_oi_call_wall_above_spot(self):
        from .support_resistance import calculate_dynamic_resistance

        zones = calculate_dynamic_resistance("NIFTY", self.expiry, 24500)
        self.assertTrue(any("oi_call_wall" in z["sources"] for z in zones))
        for z in zones:
            self.assertGreater(z["level"], 24500)

    def test_zones_sorted_nearest_first(self):
        from .support_resistance import calculate_dynamic_support

        zones = calculate_dynamic_support("NIFTY", self.expiry, 24500)
        levels = [z["level"] for z in zones]
        self.assertEqual(levels, sorted(levels, reverse=True))  # nearest (highest, below spot) first

    def test_detect_support_and_resistance_break(self):
        from .support_resistance import detect_resistance_break, detect_support_break

        support_zones = [{"level": 24300, "sources": ["oi_put_wall"], "confluence_count": 1, "details": []}]
        resistance_zones = [{"level": 24600, "sources": ["oi_call_wall"], "confluence_count": 1, "details": []}]

        self.assertFalse(detect_support_break(24350, support_zones))
        self.assertTrue(detect_support_break(24200, support_zones))
        self.assertFalse(detect_resistance_break(24550, resistance_zones))
        self.assertTrue(detect_resistance_break(24700, resistance_zones))
        self.assertFalse(detect_support_break(24500, []))


class LiquidityScoreTests(TestCase):
    """apps.options.liquidity -- continuous 0-1 scoring, distinct from apps.risk.engine's hard pass/fail gate."""

    def test_tight_spread_high_oi_scores_near_one(self):
        from .liquidity import calculate_liquidity_score

        result = calculate_liquidity_score(bid=109.9, ask=110.1, open_interest=50000, volume=20000)
        self.assertIsNotNone(result["liquidity_score"])
        self.assertGreater(result["liquidity_score"], 0.9)

    def test_wide_spread_low_oi_scores_low(self):
        from .liquidity import calculate_liquidity_score

        result = calculate_liquidity_score(bid=80.0, ask=140.0, open_interest=10, volume=0)
        self.assertIsNotNone(result["liquidity_score"])
        self.assertLess(result["liquidity_score"], 0.3)

    def test_missing_input_yields_none_overall_score(self):
        from .liquidity import calculate_liquidity_score

        result = calculate_liquidity_score(bid=None, ask=None, open_interest=5000, volume=1000)
        self.assertIsNone(result["liquidity_score"])
        self.assertIsNotNone(result["oi_score"])  # component that WAS available is still reported

    def test_estimate_slippage_is_half_spread(self):
        from .liquidity import estimate_slippage

        self.assertEqual(estimate_slippage(109.0, 111.0), 1.0)
        self.assertIsNone(estimate_slippage(None, 111.0))


class PremiumEfficiencyTests(TestCase):
    """apps.options.premium_efficiency -- intrinsic/time value plus premium-vs-realized-vol deviation."""

    def test_intrinsic_and_time_value_for_itm_call(self):
        from .premium_efficiency import calculate_intrinsic_value, calculate_time_value

        intrinsic = calculate_intrinsic_value(spot=24550, strike=24400, option_type="CE")
        self.assertEqual(intrinsic, 150.0)
        self.assertEqual(calculate_time_value(option_price=180.0, intrinsic_value=intrinsic), 30.0)

    def test_intrinsic_value_never_negative_for_otm(self):
        from .premium_efficiency import calculate_intrinsic_value

        self.assertEqual(calculate_intrinsic_value(spot=24300, strike=24400, option_type="CE"), 0.0)
        self.assertEqual(calculate_intrinsic_value(spot=24500, strike=24400, option_type="PE"), 0.0)

    def test_calculate_premium_deviation_labels_richness(self):
        from .premium_efficiency import calculate_premium_deviation

        self.assertEqual(calculate_premium_deviation(120.0, 100.0)["richness"], "rich")
        self.assertEqual(calculate_premium_deviation(80.0, 100.0)["richness"], "cheap")
        self.assertEqual(calculate_premium_deviation(102.0, 100.0)["richness"], "fair")
        self.assertEqual(calculate_premium_deviation(None, 100.0)["richness"], "unavailable")

    def test_calculate_realized_volatility_from_real_daily_candles(self):
        from apps.market_data.models import HistoricalData

        from .premium_efficiency import calculate_realized_volatility

        base = timezone.now() - timedelta(days=25)
        price = 24000
        for i in range(22):
            # A small deterministic oscillation -- real (nonzero) daily
            # returns, not a flat series (which would give exactly-zero
            # realized vol and not actually exercise the stdev math).
            price = price + (50 if i % 2 == 0 else -30)
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="1d", timestamp=base + timedelta(days=i),
                open=price, high=price + 20, low=price - 20, close=price,
                volume=100000, source="test",
            )

        vol = calculate_realized_volatility("NIFTY", timeframe="1d", lookback_days=20)
        self.assertIsNotNone(vol)
        self.assertGreater(vol, 0)

    def test_calculate_realized_volatility_none_with_too_little_history(self):
        from .premium_efficiency import calculate_realized_volatility

        self.assertIsNone(calculate_realized_volatility("NIFTY", timeframe="1d", lookback_days=20))

    def test_calculate_premium_deviation_for_contract_with_real_fixture(self):
        from apps.market_data.models import HistoricalData

        from .premium_efficiency import calculate_premium_deviation_for_contract

        base = timezone.now() - timedelta(days=25)
        price = 24000
        for i in range(22):
            price = price + (50 if i % 2 == 0 else -30)
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="1d", timestamp=base + timedelta(days=i),
                open=price, high=price + 20, low=price - 20, close=price,
                volume=100000, source="test",
            )

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=24400,
            option_type="CE", symbol_token="tok_ce_24400b", tradingsymbol="NIFTY24400CE2", lot_size=25,
        )
        result = calculate_premium_deviation_for_contract(contract, spot=24500, market_price=313.73)
        self.assertIsNotNone(result["theoretical_value"])
        self.assertIsNotNone(result["realized_vol_pct"])
        self.assertIn(result["richness"], ("rich", "cheap", "fair"))
        self.assertEqual(result["intrinsic_value"], 100.0)  # 24500 - 24400


class OiIntelligenceTests(TestCase):
    """apps.options.oi_intelligence -- confidence-scored buildup, OI concentration, migration, and the smart-money proxy."""

    def setUp(self):
        self.expiry = date.today() + timedelta(days=7)

    def _make_snapshot_series(self, contract, ltp_series, oi_series, volume_series, minutes_apart=5):
        base = timezone.now() - timedelta(minutes=minutes_apart * (len(ltp_series) - 1))
        rows = []
        for i, (ltp, oi, vol) in enumerate(zip(ltp_series, oi_series, volume_series)):
            rows.append(OptionChainSnapshot.objects.create(
                contract=contract, timestamp=base + timedelta(minutes=minutes_apart * i),
                ltp=Decimal(str(ltp)), open_interest=oi, change_in_oi=0, volume=vol,
            ))
        return rows

    def test_classify_buildup_with_confidence_bullish_call_buildup(self):
        from .oi_intelligence import classify_buildup_with_confidence

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="CE",
            symbol_token="tok_ce_24500", tradingsymbol="NIFTY24500CE", lot_size=25,
        )
        # Rising price + rising OI + elevated volume -> buildup_bullish, high confidence.
        self._make_snapshot_series(contract, ltp_series=[100, 105, 120], oi_series=[5000, 5200, 6000], volume_series=[500, 600, 2000])

        result = classify_buildup_with_confidence("NIFTY", self.expiry, "CE")
        self.assertEqual(result["classification"], "buildup_bullish")
        self.assertIsNotNone(result["confidence"])
        self.assertGreaterEqual(result["confidence"], 0.3)
        self.assertLessEqual(result["confidence"], 0.95)

    def test_classify_buildup_with_confidence_insufficient_data(self):
        from .oi_intelligence import classify_buildup_with_confidence

        result = classify_buildup_with_confidence("NIFTY", self.expiry, "CE")
        self.assertIsNone(result["classification"])
        self.assertIsNone(result["confidence"])

    def test_calculate_oi_concentration_matches_hand_computed_value(self):
        from .oi_intelligence import calculate_oi_concentration

        for strike, oi in [(24400, 1000), (24500, 6000), (24600, 3000), (24700, 500)]:
            contract = OptionContract.objects.create(
                underlying="NIFTY", expiry=self.expiry, strike=strike, option_type="CE",
                symbol_token=f"tok_ce_{strike}", tradingsymbol=f"NIFTY{strike}CE", lot_size=25,
            )
            OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("100"), open_interest=oi, change_in_oi=0, volume=500)

        # total OI = 10500, top_n=2 (24500 + 24600) = 9000 -> 9000/10500 = 85.71%
        result = calculate_oi_concentration("NIFTY", self.expiry, "CE", top_n=2)
        self.assertAlmostEqual(result["concentration_pct"], 85.71, places=1)
        self.assertEqual(len(result["top_strikes"]), 2)

    def test_detect_oi_migration_reports_shift(self):
        from .oi_intelligence import detect_oi_migration

        c1 = OptionContract.objects.create(underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="CE", symbol_token="tok_m1", tradingsymbol="NIFTY24500CEM", lot_size=25)
        c2 = OptionContract.objects.create(underlying="NIFTY", expiry=self.expiry, strike=24600, option_type="CE", symbol_token="tok_m2", tradingsymbol="NIFTY24600CEM", lot_size=25)

        t1 = timezone.now() - timedelta(minutes=10)
        t2 = timezone.now() - timedelta(minutes=5)
        t3 = timezone.now()
        # t1: c1 has the highest OI. t2/t3: c2 takes over -> a real migration.
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t1, ltp=Decimal("100"), open_interest=8000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c2, timestamp=t1, ltp=Decimal("80"), open_interest=2000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t2, ltp=Decimal("100"), open_interest=3000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c2, timestamp=t2, ltp=Decimal("80"), open_interest=9000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t3, ltp=Decimal("100"), open_interest=3100, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c2, timestamp=t3, ltp=Decimal("80"), open_interest=9500, change_in_oi=0, volume=100)

        result = detect_oi_migration("NIFTY", self.expiry, "CE", lookback_points=3)
        self.assertTrue(result["migrated"])
        self.assertEqual(result["peak_strike_sequence"], [24500.0, 24600.0, 24600.0])

    def test_detect_oi_migration_no_shift_when_peak_holds(self):
        from .oi_intelligence import detect_oi_migration

        c1 = OptionContract.objects.create(underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="CE", symbol_token="tok_h1", tradingsymbol="NIFTY24500CEH", lot_size=25)
        t1, t2 = timezone.now() - timedelta(minutes=5), timezone.now()
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t1, ltp=Decimal("100"), open_interest=5000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t2, ltp=Decimal("100"), open_interest=5200, change_in_oi=0, volume=100)

        result = detect_oi_migration("NIFTY", self.expiry, "CE", lookback_points=3)
        self.assertFalse(result["migrated"])

    def test_institutional_positioning_proxy_always_labeled_proxy(self):
        from .oi_intelligence import institutional_positioning_proxy

        result = institutional_positioning_proxy("NIFTY", self.expiry, "bullish")
        self.assertEqual(result["label"], "PROXY")
        self.assertIn(result["leaning"], ("insufficient_data",))  # no data seeded in this test

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="CE",
            symbol_token="tok_proxy", tradingsymbol="NIFTY24500CEP", lot_size=25,
        )
        self._make_snapshot_series(contract, ltp_series=[100, 110, 125], oi_series=[4000, 4500, 5500], volume_series=[400, 500, 1500])
        result = institutional_positioning_proxy("NIFTY", self.expiry, "bullish")
        self.assertEqual(result["label"], "PROXY")
        self.assertEqual(result["leaning"], "large_holders_likely_accumulating")


class ConfirmationTests(TestCase):
    """apps.options.confirmation -- structured directional/setup-quality factor breakdown + conflict detection."""

    def test_evaluate_trend_bullish_and_bearish(self):
        from .confirmation import _evaluate_trend

        bullish_ind = {"close": 24500, "ema9_slope": 5.0, "ema21_slope": 3.0}
        self.assertEqual(_evaluate_trend(bullish_ind, "bullish")["signal"], "bullish")

        bearish_ind = {"close": 24500, "ema9_slope": -5.0, "ema21_slope": -3.0}
        self.assertEqual(_evaluate_trend(bearish_ind, "bullish")["signal"], "bearish")

        mixed_ind = {"close": 24500, "ema9_slope": 5.0, "ema21_slope": -3.0}
        self.assertEqual(_evaluate_trend(mixed_ind, "bullish")["signal"], "neutral")

        self.assertEqual(_evaluate_trend(None, "bullish")["signal"], "unavailable")

    def test_evaluate_momentum(self):
        from .confirmation import _evaluate_momentum

        self.assertEqual(_evaluate_momentum({"close": 100, "macd_hist": 2.0}, "bullish")["signal"], "bullish")
        self.assertEqual(_evaluate_momentum({"close": 100, "macd_hist": -2.0}, "bullish")["signal"], "bearish")

    def test_evaluate_volume_requires_elevated_volume_and_direction(self):
        from .confirmation import _evaluate_volume

        low_vol = {"relative_volume": 1.0, "macd_hist": 2.0}
        self.assertEqual(_evaluate_volume(low_vol, "bullish")["signal"], "neutral")

        elevated_bullish = {"relative_volume": 2.0, "macd_hist": 2.0}
        self.assertEqual(_evaluate_volume(elevated_bullish, "bullish")["signal"], "bullish")

        elevated_bearish = {"relative_volume": 2.0, "macd_hist": -2.0}
        self.assertEqual(_evaluate_volume(elevated_bearish, "bullish")["signal"], "bearish")

    def test_evaluate_greeks_sweet_spot(self):
        from .confirmation import _evaluate_greeks

        self.assertEqual(_evaluate_greeks(0.5)["signal"], "favorable")
        self.assertEqual(_evaluate_greeks(0.1)["signal"], "unfavorable")
        self.assertEqual(_evaluate_greeks(None)["signal"], "unavailable")

    def test_evaluate_liquidity_and_risk_reward(self):
        from .confirmation import _evaluate_liquidity, _evaluate_risk_reward

        self.assertEqual(_evaluate_liquidity(0.8)["signal"], "favorable")
        self.assertEqual(_evaluate_liquidity(0.3)["signal"], "unfavorable")
        self.assertEqual(_evaluate_risk_reward(2.0)["signal"], "favorable")
        self.assertEqual(_evaluate_risk_reward(0.8)["signal"], "unfavorable")

    def test_detect_signal_conflict_levels(self):
        from .confirmation import detect_signal_conflict

        all_agree = {
            "trend": {"signal": "bullish"}, "momentum": {"signal": "bullish"},
            "oi": {"signal": "neutral"}, "volume": {"signal": "unavailable"}, "skew": {"signal": "bullish"},
        }
        level, _ = detect_signal_conflict(all_agree, "bullish")
        self.assertEqual(level, "CONFLICT_LOW")

        one_disagrees = {
            "trend": {"signal": "bullish"}, "momentum": {"signal": "bullish"},
            "oi": {"signal": "bearish"}, "volume": {"signal": "neutral"}, "skew": {"signal": "neutral"},
        }
        level, _ = detect_signal_conflict(one_disagrees, "bullish")
        self.assertEqual(level, "CONFLICT_MEDIUM")

        majority_disagree = {
            "trend": {"signal": "bearish"}, "momentum": {"signal": "bearish"},
            "oi": {"signal": "bearish"}, "volume": {"signal": "bullish"}, "skew": {"signal": "neutral"},
        }
        level, _ = detect_signal_conflict(majority_disagree, "bullish")
        self.assertEqual(level, "CONFLICT_HIGH")

        all_unavailable = {k: {"signal": "unavailable"} for k in ("trend", "momentum", "oi", "volume", "skew")}
        level, detail = detect_signal_conflict(all_unavailable, "bullish")
        self.assertEqual(level, "CONFLICT_LOW")
        self.assertIn("No directional factors available", detail)

    def test_evaluate_multi_signal_confirmation_structure_with_real_fixture(self):
        from apps.market_data.models import HistoricalData

        from .confirmation import evaluate_multi_signal_confirmation

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_conf_ce", tradingsymbol="NIFTY24400CECONF", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)

        ind = {"close": 24500, "ema9_slope": 5.0, "ema21_slope": 3.0, "macd_hist": 2.0, "relative_volume": 1.5}
        result = evaluate_multi_signal_confirmation(
            "NIFTY", expiry, "bullish", ind, 24500, delta=0.5, liquidity_score=0.7, risk_reward=1.8,
        )
        self.assertEqual(set(result["directional_factors"].keys()), {"trend", "momentum", "oi", "volume", "skew"})
        self.assertEqual(set(result["setup_quality_factors"].keys()), {"greeks", "liquidity", "expected_move", "risk_reward"})
        self.assertIn(result["conflict_level"], ("CONFLICT_LOW", "CONFLICT_MEDIUM", "CONFLICT_HIGH"))
        self.assertEqual(result["setup_quality_factors"]["greeks"]["signal"], "favorable")
        self.assertEqual(result["setup_quality_factors"]["risk_reward"]["signal"], "favorable")


class AnomalyDetectionTests(TestCase):
    """apps.options.anomaly_detection -- z-score vs. rolling historical baseline, hand-verified arithmetic."""

    def test_z_score_anomaly_hand_computed(self):
        from .anomaly_detection import _z_score_anomaly

        # baseline mean=10, std=sqrt(2)=1.4142; current=20 -> z=7.07
        result = _z_score_anomaly(20, [10, 12, 8, 11, 9])
        self.assertAlmostEqual(result["z_score"], 7.07, places=2)
        self.assertTrue(result["is_anomaly"])

        result = _z_score_anomaly(10.5, [10, 12, 8, 11, 9])
        self.assertFalse(result["is_anomaly"])

    def test_z_score_anomaly_insufficient_history(self):
        from .anomaly_detection import _z_score_anomaly

        result = _z_score_anomaly(20, [10, 12])
        self.assertIsNone(result["z_score"])
        self.assertFalse(result["is_anomaly"])

    def test_z_score_anomaly_zero_variance_baseline(self):
        from .anomaly_detection import _z_score_anomaly

        result = _z_score_anomaly(50, [10, 10, 10, 10, 10])
        self.assertIsNone(result["z_score"])
        self.assertFalse(result["is_anomaly"])

    def test_detect_volume_and_oi_and_iv_and_premium_anomaly_with_real_fixture(self):
        from .anomaly_detection import (
            detect_iv_anomaly, detect_oi_change_anomaly, detect_premium_anomaly, detect_volume_anomaly,
        )

        expiry = date.today() + timedelta(days=7)
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24500, option_type="CE",
            symbol_token="tok_anomaly", tradingsymbol="NIFTY24500CEA", lot_size=25,
        )
        base = timezone.now() - timedelta(minutes=50)
        # 5 normal baseline readings, then one clear volume/OI-change/IV outlier as "current".
        for i, (vol, oi_chg, iv) in enumerate([(500, 100, 15.0), (520, 90, 15.2), (480, 110, 14.8), (510, 95, 15.1), (490, 105, 14.9)]):
            OptionChainSnapshot.objects.create(
                contract=contract, timestamp=base + timedelta(minutes=5 * i),
                ltp=Decimal("100.0"), open_interest=5000, change_in_oi=oi_chg, volume=vol, iv=iv,
            )
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=base + timedelta(minutes=30),
            ltp=Decimal("100.0"), open_interest=5000, change_in_oi=5000, volume=50000, iv=60.0,
        )

        self.assertTrue(detect_volume_anomaly(contract)["is_anomaly"])
        self.assertTrue(detect_oi_change_anomaly(contract)["is_anomaly"])
        self.assertTrue(detect_iv_anomaly(contract)["is_anomaly"])

    def test_detect_premium_anomaly_uses_change_series_not_raw_levels(self):
        from .anomaly_detection import detect_premium_anomaly

        expiry = date.today() + timedelta(days=7)
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24600, option_type="CE",
            symbol_token="tok_prem_anomaly", tradingsymbol="NIFTY24600CEA", lot_size=25,
        )
        base = timezone.now() - timedelta(minutes=60)
        # Steady +2% moves each step (normal drift), then one huge +40% jump.
        price = 100.0
        for i in range(6):
            OptionChainSnapshot.objects.create(
                contract=contract, timestamp=base + timedelta(minutes=5 * i),
                ltp=Decimal(str(round(price, 2))), open_interest=5000, change_in_oi=0, volume=500,
            )
            price *= 1.02
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=base + timedelta(minutes=35),
            ltp=Decimal(str(round(price * 1.40, 2))), open_interest=5000, change_in_oi=0, volume=500,
        )

        result = detect_premium_anomaly(contract)
        self.assertTrue(result["is_anomaly"])

    def test_bid_ask_imbalance_anomaly_is_honestly_unavailable(self):
        from .anomaly_detection import detect_bid_ask_imbalance_anomaly

        result = detect_bid_ask_imbalance_anomaly()
        self.assertFalse(result["available"])
        self.assertIn("reason", result)


class OrderFlowStubTests(TestCase):
    """apps.options.order_flow -- every function honestly unavailable, no fabricated numbers."""

    def test_every_function_reports_unavailable(self):
        from . import order_flow

        for fn_name in (
            "calculate_bid_ask_imbalance", "detect_aggressive_buying", "detect_aggressive_selling",
            "calculate_volume_acceleration", "detect_order_flow_shift", "detect_liquidity_withdrawal",
        ):
            result = getattr(order_flow, fn_name)()
            self.assertFalse(result["available"])
            self.assertIn("reason", result)


class StrategySelectorTests(TestCase):
    """apps.options.strategy_selector -- classification only, only LONG_CALL/LONG_PUT/NO_TRADE are ever executable."""

    def test_no_direction_is_no_trade(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy(None, "TRENDING", 40.0)
        self.assertEqual(result["strategy"], "NO_TRADE")
        self.assertTrue(result["executable"])

    def test_high_conflict_is_no_trade(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "TRENDING", 40.0, conflict_level="CONFLICT_HIGH")
        self.assertEqual(result["strategy"], "NO_TRADE")

    def test_trending_moderate_iv_gives_executable_long_option(self):
        from .strategy_selector import classify_strategy

        bullish = classify_strategy("bullish", "TRENDING", 40.0, expected_move_position="inside_range")
        self.assertEqual(bullish["strategy"], "LONG_CALL")
        self.assertTrue(bullish["executable"])

        bearish = classify_strategy("bearish", "TRENDING_BEARISH", 40.0, expected_move_position="inside_range")
        self.assertEqual(bearish["strategy"], "LONG_PUT")
        self.assertTrue(bearish["executable"])

    def test_trending_high_iv_is_spread_not_executable(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "TRENDING", 85.0, expected_move_position="inside_range")
        self.assertEqual(result["strategy"], "BULL_CALL_SPREAD")
        self.assertFalse(result["executable"])

    def test_move_already_extended_avoids_long_option_even_low_iv(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "TRENDING", 40.0, expected_move_position="outside_upper_range")
        self.assertNotEqual(result["strategy"], "LONG_CALL")
        self.assertFalse(result["executable"])

    def test_non_trending_high_iv_is_iron_condor(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "SIDEWAYS", 85.0)
        self.assertEqual(result["strategy"], "IRON_CONDOR")
        self.assertFalse(result["executable"])

    def test_non_trending_low_iv_is_straddle(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "SIDEWAYS", 10.0)
        self.assertEqual(result["strategy"], "STRADDLE")
        self.assertFalse(result["executable"])

    def test_fallback_is_no_trade(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "SIDEWAYS", 50.0)  # neither high nor low IV, non-trending
        self.assertEqual(result["strategy"], "NO_TRADE")
        self.assertTrue(result["executable"])

    def test_executable_strategies_constant_matches_actual_execution_capability(self):
        from .strategy_selector import EXECUTABLE_STRATEGIES

        self.assertEqual(EXECUTABLE_STRATEGIES, frozenset({"LONG_CALL", "LONG_PUT", "NO_TRADE"}))


class SignalScoringTests(TestCase):
    """apps.options.signal_scoring -- hand-verified weighted arithmetic, unavailable factors excluded and re-normalized."""

    def test_calculate_signal_score_hand_computed(self):
        from .signal_scoring import calculate_signal_score

        weights = {
            "trend": 0.18, "momentum": 0.12, "oi": 0.15, "volume": 0.07, "skew": 0.08,
            "greeks": 0.10, "liquidity": 0.13, "expected_move": 0.08, "risk_reward": 0.09,
        }
        confirmation_result = {
            "directional_factors": {
                "trend": {"signal": "bullish"}, "momentum": {"signal": "bearish"},
                "oi": {"signal": "neutral"}, "volume": {"signal": "unavailable"}, "skew": {"signal": "unavailable"},
            },
            "setup_quality_factors": {
                "greeks": {"signal": "favorable"}, "liquidity": {"signal": "favorable"},
                "expected_move": {"signal": "neutral"}, "risk_reward": {"signal": "unfavorable"},
            },
        }
        result = calculate_signal_score(confirmation_result, weights=weights)
        self.assertAlmostEqual(result["total_score"], 0.6176, places=3)
        self.assertEqual(set(result["factors_excluded"]), {"volume", "skew"})

    def test_all_unavailable_gives_none_not_zero(self):
        from .signal_scoring import calculate_signal_score

        confirmation_result = {
            "directional_factors": {k: {"signal": "unavailable"} for k in ("trend", "momentum", "oi", "volume", "skew")},
            "setup_quality_factors": {k: {"signal": "unavailable"} for k in ("greeks", "liquidity", "expected_move", "risk_reward")},
        }
        result = calculate_signal_score(confirmation_result)
        self.assertIsNone(result["total_score"])

    def test_uses_db_backed_weights_by_default(self):
        from .models import get_scoring_weights
        from .signal_scoring import calculate_signal_score

        confirmation_result = {
            "directional_factors": {"trend": {"signal": "bullish"}},
            "setup_quality_factors": {},
        }
        result = calculate_signal_score(confirmation_result)
        self.assertEqual(result["weights_used"], get_scoring_weights().as_dict())
        self.assertEqual(result["total_score"], 1.0)  # only factor available is bullish=1.0


class SignalExplanationTests(TestCase):
    """apps.options.signal_explanation -- itemized positive/negative/risk-flag lists, neutral/unavailable omitted."""

    def test_build_signal_explanation_itemizes_correctly(self):
        from .signal_explanation import build_signal_explanation

        confirmation_result = {
            "directional_factors": {
                "trend": {"signal": "bullish", "detail": "EMA9/21 rising"},
                "momentum": {"signal": "bearish", "detail": "MACD histogram negative"},
                "oi": {"signal": "neutral", "detail": "no strong OI signal"},
            },
            "setup_quality_factors": {
                "liquidity": {"signal": "favorable", "detail": "liquidity score 0.85"},
            },
            "conflict_level": "CONFLICT_MEDIUM", "conflict_detail": "1/2 factors disagree.",
        }
        result = build_signal_explanation(confirmation_result)
        self.assertEqual(result["positive_factors"], ["Trend: EMA9/21 rising", "Liquidity: liquidity score 0.85"])
        self.assertEqual(result["negative_factors"], ["Momentum: MACD histogram negative"])
        self.assertEqual(len(result["risk_flags"]), 1)
        self.assertIn("Conflict Medium", result["risk_flags"][0])

    def test_anomalies_surface_only_when_flagged(self):
        from .signal_explanation import build_signal_explanation

        confirmation_result = {"directional_factors": {}, "setup_quality_factors": {}}
        anomalies = [
            {"is_anomaly": True, "detail": "volume z=4.2"},
            {"is_anomaly": False, "detail": "iv z=0.5"},
        ]
        result = build_signal_explanation(confirmation_result, anomalies=anomalies)
        self.assertEqual(len(result["risk_flags"]), 1)
        self.assertIn("volume z=4.2", result["risk_flags"][0])


class FinalSignalTests(TestCase):
    """apps.options.final_signal.resolve_final_signal -- the Section-43-style assembled object, real contract identity or explicit None throughout."""

    def test_rejected_signal_with_no_contract_is_data_invalid(self):
        from .final_signal import resolve_final_signal

        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type="no_trade", entry_price=Decimal("24500"), stop_loss=Decimal("24500"),
            total_score=0, technical_score=0, sentiment_score=0, risk_score=0, options_score=0,
            regime="sideways", status="rejected", rejection_stage="data_quality",
            reason="OPTION DATA UNAVAILABLE: test",
        )
        result = resolve_final_signal(signal)
        self.assertEqual(result["status"], "DATA_INVALID")
        self.assertIsNone(result["tradingSymbol"])
        self.assertIsNone(result["instrumentToken"])
        self.assertIsNone(result["strike"])
        self.assertEqual(result["decision"], "NO_TRADE")

    def test_approved_signal_with_real_contract_is_signal_ready(self):
        from apps.market_data.models import HistoricalData

        from .final_signal import resolve_final_signal

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_final_ce", tradingsymbol="NIFTY24400CEFINAL", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)

        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type="buy", entry_price=Decimal("313.73"), stop_loss=Decimal("280.0"),
            target_1=Decimal("360.0"), target_2=Decimal("400.0"), position_size=25,
            option_side="CE", strike_price=Decimal("24400"), option_contract=contract,
            total_score=0.7, technical_score=0.7, sentiment_score=0.1, risk_score=1.0, options_score=0.6,
            regime="trending", status="approved", reason="test approved signal",
        )
        result = resolve_final_signal(signal)
        self.assertEqual(result["status"], "SIGNAL_READY")
        self.assertEqual(result["tradingSymbol"], "NIFTY24400CEFINAL")
        self.assertEqual(result["instrumentToken"], "tok_final_ce")
        self.assertEqual(result["strike"], 24400.0)
        self.assertEqual(result["optionType"], "CE")
        self.assertEqual(result["direction"], "bullish")
        self.assertEqual(result["decision"], "BUY")
        self.assertIsNotNone(result["delta"])
        self.assertIsNotNone(result["riskReward"])
        self.assertIsInstance(result["support"], list)
        self.assertIsInstance(result["resistance"], list)
        self.assertIn("expected_move", result["expectedMove"])
        self.assertIn(result["strategy"], ("LONG_CALL", "NO_TRADE", "BULL_CALL_SPREAD"))


class FinalSignalViewTests(APITestCase):
    """GET /api/options/final-signal/ -- 404 with no signals, 200 with the assembled object otherwise."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader_final", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Trader")[0])
        self.client.force_authenticate(self.user)

    def test_404_when_no_signals_exist(self):
        response = self.client.get("/api/options/final-signal/", {"underlying": "NIFTY"})
        self.assertEqual(response.status_code, 404)

    def test_200_with_latest_signal(self):
        TradingSignal.objects.create(
            symbol="NIFTY", signal_type="no_trade", entry_price=Decimal("24500"), stop_loss=Decimal("24500"),
            total_score=0, technical_score=0, sentiment_score=0, risk_score=0, options_score=0,
            regime="sideways", status="rejected", reason="test",
        )
        response = self.client.get("/api/options/final-signal/", {"underlying": "NIFTY"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("status", response.data)
        self.assertIn("decision", response.data)


class FeatureStoreTests(TestCase):
    """apps.options.feature_store.build_feature_vector -- real contract fixture populates scores/greeks, no contract leaves them None."""

    def test_no_contract_leaves_scores_and_greeks_none(self):
        from .feature_store import build_feature_vector

        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type="no_trade", entry_price=Decimal("24500"), stop_loss=Decimal("24500"),
            total_score=0, technical_score=0, sentiment_score=0, risk_score=0, options_score=0,
            regime="sideways", status="rejected", reason="test",
        )
        vector = build_feature_vector(signal)
        self.assertIsNone(vector["delta"])
        self.assertIsNone(vector["trendScore"])
        self.assertIsNone(vector["orderFlowScore"])  # always None -- honest stub, no data source
        self.assertEqual(vector["marketRegime"], "sideways")
        self.assertIn(vector["timeOfDay"], ("opening", "morning", "midday", "afternoon", "closing", "closed", "unknown"))

    def test_real_contract_populates_greeks_and_scores(self):
        from apps.market_data.models import HistoricalData

        from .feature_store import build_feature_vector

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_fs_ce", tradingsymbol="NIFTY24400CEFS", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)

        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type="buy", entry_price=Decimal("313.73"), stop_loss=Decimal("280.0"),
            target_1=Decimal("360.0"), position_size=25, option_side="CE", strike_price=Decimal("24400"),
            option_contract=contract,
            total_score=0.7, technical_score=0.7, sentiment_score=0.1, risk_score=1.0, options_score=0.6,
            regime="trending", status="approved", reason="test",
        )
        vector = build_feature_vector(signal)
        self.assertIsNotNone(vector["delta"])
        self.assertIsNotNone(vector["gamma"])
        self.assertEqual(vector["expiry"], expiry.isoformat())
        self.assertEqual(vector["strikeDistance"], 100.0)
        self.assertIsNotNone(vector["riskRewardScore"])


def _make_contract(underlying="NIFTY", expiry=None, strike=24500, option_type="CE", token="tok_candle_ce"):
    expiry = expiry or (date.today() + timedelta(days=3))
    return OptionContract.objects.create(
        underlying=underlying, expiry=expiry, strike=strike, option_type=option_type,
        symbol_token=token, tradingsymbol=f"{underlying}{strike}{option_type}", lot_size=25,
    )


class ContractResolutionTests(TestCase):
    """
    apps.options.candle_service.resolve_contract -- the ONE place a
    click's {underlying, expiry, strike, option_type} (or an already-
    known contract id/token) turns into a real OptionContract row.
    Covers scenarios 1/2 (exact CE vs. exact PE never cross-resolve).
    """

    def setUp(self):
        self.expiry = date.today() + timedelta(days=3)
        self.ce = _make_contract(strike=24500, option_type="CE", token="tok_ce_24500")
        self.pe = _make_contract(strike=24500, option_type="PE", token="tok_pe_24500")

    def test_resolves_exact_ce_not_pe(self):
        from .candle_service import resolve_contract

        resolved = resolve_contract(underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="CE")
        self.assertEqual(resolved.id, self.ce.id)
        self.assertNotEqual(resolved.id, self.pe.id)

    def test_resolves_exact_pe_not_ce(self):
        from .candle_service import resolve_contract

        resolved = resolve_contract(underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="PE")
        self.assertEqual(resolved.id, self.pe.id)
        self.assertNotEqual(resolved.id, self.ce.id)

    def test_resolves_by_contract_id(self):
        from .candle_service import resolve_contract

        self.assertEqual(resolve_contract(contract_id=self.ce.id).id, self.ce.id)

    def test_resolves_by_token(self):
        from .candle_service import resolve_contract

        self.assertEqual(resolve_contract(token="tok_pe_24500").id, self.pe.id)

    def test_unknown_identity_raises_resolution_error(self):
        from .candle_service import ContractResolutionError, resolve_contract

        with self.assertRaises(ContractResolutionError):
            resolve_contract(underlying="NIFTY", expiry=self.expiry, strike=99999, option_type="CE")

    def test_no_identity_given_raises_resolution_error(self):
        from .candle_service import ContractResolutionError, resolve_contract

        with self.assertRaises(ContractResolutionError):
            resolve_contract()

    def test_garbage_strike_raises_resolution_error_not_500(self):
        from .candle_service import ContractResolutionError, resolve_contract

        with self.assertRaises(ContractResolutionError):
            resolve_contract(underlying="NIFTY", expiry=self.expiry, strike="not-a-number", option_type="CE")


class OptionCandleServiceTests(TestCase):
    """
    apps.options.candle_service.get_option_candles -- DB-first serving,
    OHLC validation, and dedupe/sort guarantees. Covers scenarios 6 (no
    duplicates), 15 (empty data handled honestly), and the "avoid a
    broker call when the DB already has valid candles" caching requirement.
    """

    def setUp(self):
        self.contract = _make_contract()

    def test_fresh_db_rows_served_without_any_broker_call(self):
        from .candle_service import get_option_candles
        from .models import OptionCandle

        OptionCandle.objects.create(
            contract=self.contract, timeframe="5m", timestamp=timezone.now(),
            open=100, high=105, low=98, close=103, volume=1000, source="angel_one",
        )
        with patch("apps.options.broker_client.get_option_chain_client") as mocked_client:
            candles = get_option_candles(self.contract, "5m")
        mocked_client.assert_not_called()
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["close"], 103.0)

    def test_empty_history_returns_empty_list_not_fake_candles(self):
        from .candle_service import get_option_candles

        with override_settings(BROKER_MODE="paper"):
            candles = get_option_candles(self.contract, "5m")
        self.assertEqual(candles, [])

    def test_stale_db_triggers_broker_fetch_and_upsert(self):
        from .candle_service import get_option_candles
        from .models import OptionCandle

        stale_ts = timezone.now() - timedelta(hours=6)
        OptionCandle.objects.create(
            contract=self.contract, timeframe="5m", timestamp=stale_ts,
            open=100, high=105, low=98, close=103, volume=1000, source="angel_one",
        )
        fresh_ts = timezone.now()
        fake_client = type("FakeClient", (), {
            "fetch_candles_for_token": lambda self, exchange, token, timeframe, lookback_days=5, to_date=None: [
                {"timestamp": fresh_ts, "open": 110, "high": 115, "low": 108, "close": 112, "volume": 500},
            ],
        })()
        with override_settings(BROKER_MODE="live"), \
             patch("apps.options.broker_client.get_option_chain_client", return_value=fake_client):
            candles = get_option_candles(self.contract, "5m")
        self.assertEqual(OptionCandle.objects.filter(contract=self.contract).count(), 2)
        self.assertAlmostEqual(candles[-1]["close"], 112.0)

    def test_invalid_ohlc_rows_are_dropped_not_saved(self):
        from .candle_service import get_option_candles
        from .models import OptionCandle

        stale_ts = timezone.now() - timedelta(hours=6)
        OptionCandle.objects.create(
            contract=self.contract, timeframe="5m", timestamp=stale_ts,
            open=100, high=105, low=98, close=103, volume=1000, source="angel_one",
        )
        bad_ts = timezone.now()
        fake_client = type("FakeClient", (), {
            "fetch_candles_for_token": lambda self, exchange, token, timeframe, lookback_days=5, to_date=None: [
                # high < low is impossible OHLC -- must be rejected, never stored.
                {"timestamp": bad_ts, "open": 100, "high": 90, "low": 95, "close": 92, "volume": 10},
            ],
        })()
        with override_settings(BROKER_MODE="live"), \
             patch("apps.options.broker_client.get_option_chain_client", return_value=fake_client):
            get_option_candles(self.contract, "5m")
        self.assertFalse(OptionCandle.objects.filter(contract=self.contract, timestamp=bad_ts).exists())

    def test_duplicate_timeframe_timestamp_never_stored_twice(self):
        from .models import OptionCandle

        ts = timezone.now()
        OptionCandle.objects.create(
            contract=self.contract, timeframe="5m", timestamp=ts,
            open=100, high=105, low=98, close=103, volume=1000, source="angel_one",
        )
        OptionCandle.objects.update_or_create(
            contract=self.contract, timeframe="5m", timestamp=ts,
            defaults={"open": 101, "high": 106, "low": 99, "close": 104, "volume": 1200, "source": "angel_one_live"},
        )
        self.assertEqual(OptionCandle.objects.filter(contract=self.contract, timeframe="5m", timestamp=ts).count(), 1)

    def test_results_sorted_ascending(self):
        from .candle_service import get_option_candles
        from .models import OptionCandle

        now = timezone.now()
        OptionCandle.objects.create(contract=self.contract, timeframe="5m", timestamp=now, open=1, high=2, low=1, close=2, volume=1)
        OptionCandle.objects.create(contract=self.contract, timeframe="5m", timestamp=now - timedelta(minutes=5), open=1, high=2, low=1, close=1.5, volume=1)
        candles = get_option_candles(self.contract, "5m")
        times = [c["time"] for c in candles]
        self.assertEqual(times, sorted(times))


class OptionCandlesViewTests(APITestCase):
    """
    GET /api/options/candles/ -- scenarios 1/2 (exact CE/PE), 19 (auth
    enforced), and honest 404 on a contract that doesn't exist (e.g.
    right after rollover), matching apps.options.candle_service's own
    "backend is the source of truth for contract identity" contract.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="candle_trader", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Trader")[0])
        self.expiry = date.today() + timedelta(days=3)
        self.ce = _make_contract(strike=24500, option_type="CE", token="tok_view_ce", expiry=self.expiry)
        self.pe = _make_contract(strike=24500, option_type="PE", token="tok_view_pe", expiry=self.expiry)
        from .models import OptionCandle

        OptionCandle.objects.create(
            contract=self.ce, timeframe="5m", timestamp=timezone.now(),
            open=100, high=105, low=98, close=103, volume=1000, source="angel_one",
        )

    def test_requires_authentication(self):
        response = self.client.get("/api/options/candles/", {
            "underlying": "NIFTY", "expiry": self.expiry.isoformat(), "strike": 24500, "option_type": "CE",
        })
        self.assertIn(response.status_code, (401, 403))

    def test_click_ce_returns_exact_ce_contract_and_candles(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/options/candles/", {
            "underlying": "NIFTY", "expiry": self.expiry.isoformat(), "strike": 24500,
            "option_type": "CE", "timeframe": "5m",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["contract"]["id"], self.ce.id)
        self.assertEqual(response.data["contract"]["option_type"], "CE")
        self.assertEqual(response.data["contract"]["token"], "tok_view_ce")
        self.assertEqual(response.data["contract"]["exchange"], "NFO")
        self.assertEqual(len(response.data["candles"]), 1)
        self.assertEqual(response.data["candles"][0]["close"], 103.0)

    def test_click_pe_never_returns_ce_data(self):
        # The PE side has zero seeded candles, so the DB-freshness check
        # would fail and get_option_candles would otherwise attempt a
        # REAL Angel One call here -- BROKER_MODE=paper keeps this test
        # (which is only about CE/PE never cross-contaminating, not
        # about broker fetch behavior) from ever touching the network,
        # same reasoning ContractSyncTests' own docstring gives.
        self.client.force_authenticate(self.user)
        with override_settings(BROKER_MODE="paper"):
            response = self.client.get("/api/options/candles/", {
                "underlying": "NIFTY", "expiry": self.expiry.isoformat(), "strike": 24500,
                "option_type": "PE", "timeframe": "5m",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["contract"]["id"], self.pe.id)
        self.assertEqual(response.data["candles"], [])  # no candles seeded for the PE side

    def test_resolve_by_contract_id(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/options/candles/", {"contract": self.ce.id, "timeframe": "5m"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["contract"]["id"], self.ce.id)

    def test_nonexistent_contract_returns_404_not_empty_200(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/options/candles/", {
            "underlying": "NIFTY", "expiry": self.expiry.isoformat(), "strike": 99999,
            "option_type": "CE", "timeframe": "5m",
        })
        self.assertEqual(response.status_code, 404)

    def test_unsupported_timeframe_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/options/candles/", {"contract": self.ce.id, "timeframe": "7m"})
        self.assertEqual(response.status_code, 400)


class OptionCandleAggregatorTests(TestCase):
    """
    apps.options.candle_aggregator.OptionCandleAggregator -- bucket
    boundaries, rollover-persists-the-closed-bar, and Channels group
    naming/isolation (scenario 8: a new candle starts at the correct
    timeframe boundary).
    """

    def test_bucket_rollover_persists_previous_bar_and_opens_new_one(self):
        from .candle_aggregator import OptionCandleAggregator
        from .models import OptionCandle

        contract = _make_contract(token="tok_agg")
        aggregator = OptionCandleAggregator(timeframes=["5m"])

        t0 = _ist(2026, 8, 19, 10, 0)
        aggregator.on_tick(contract.id, 100.0, 1000, t0)
        aggregator.on_tick(contract.id, 102.0, 1200, t0 + timedelta(minutes=1))

        t1 = t0 + timedelta(minutes=5)  # next 5m bucket
        aggregator.on_tick(contract.id, 108.0, 1500, t1)

        saved = list(OptionCandle.objects.filter(contract=contract, timeframe="5m").order_by("timestamp"))
        self.assertEqual(len(saved), 2)
        self.assertEqual(float(saved[0].close), 102.0)  # first bucket froze at its last tick before rollover
        self.assertEqual(float(saved[1].open), 108.0)   # new bucket opened fresh, not carrying over the old close

    def test_group_name_is_contract_and_timeframe_specific(self):
        from .candle_aggregator import group_name

        self.assertEqual(group_name(42, "5m"), "option_candles_42_5m")
        self.assertNotEqual(group_name(42, "5m"), group_name(43, "5m"))
        self.assertNotEqual(group_name(42, "5m"), group_name(42, "1m"))


class OptionCandleConsumerTests(TestCase):
    """
    apps.options.consumers.OptionCandleConsumer -- verifies group
    isolation actually holds at the WebSocket transport layer, not just
    in the group-naming helper: a broadcast to one contract+timeframe's
    group must never reach a browser connected to a DIFFERENT one.
    """

    @override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
    def test_only_matching_contract_timeframe_group_receives_broadcast(self):
        import asyncio
        from types import SimpleNamespace

        from channels.layers import get_channel_layer
        from channels.routing import URLRouter
        from channels.testing import WebsocketCommunicator

        from .routing import websocket_urlpatterns

        async def authenticated_scope(app, scope, receive, send):
            scope = dict(scope)
            scope["user"] = SimpleNamespace(is_authenticated=True, is_active=True)
            await app(scope, receive, send)

        async def scenario():
            router = URLRouter(websocket_urlpatterns)

            async def app(scope, receive, send):
                await authenticated_scope(router, scope, receive, send)

            comm_a = WebsocketCommunicator(app, "/ws/options/candles/1/5m/")
            comm_b = WebsocketCommunicator(app, "/ws/options/candles/2/5m/")
            try:
                connected_a, _ = await comm_a.connect()
                connected_b, _ = await comm_b.connect()
                self.assertTrue(connected_a)
                self.assertTrue(connected_b)

                channel_layer = get_channel_layer()
                await channel_layer.group_send(
                    "option_candles_1_5m",
                    {"type": "candle_update", "data": {"contract_id": 1, "timeframe": "5m", "close": 123.0}},
                )

                message = await comm_a.receive_json_from(timeout=2)
                self.assertEqual(message["contract_id"], 1)
                self.assertTrue(await comm_b.receive_nothing(timeout=0.5))
            finally:
                await comm_a.disconnect()
                await comm_b.disconnect()

        asyncio.run(scenario())
