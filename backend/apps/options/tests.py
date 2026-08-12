from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from .metrics import compute_max_pain, compute_pcr, strike_support_resistance
from .models import OptionChainSnapshot, OptionContract


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


class RunIndexDirectionStrategyTaskTests(TestCase):
    def test_skips_outside_market_hours(self):
        from unittest.mock import patch

        with patch("apps.options.tasks.is_market_open", return_value=(False, "market closed")):
            from .tasks import run_index_direction_strategy

            result = run_index_direction_strategy()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "market closed")


class SyncWatchlistOptionContractsTests(TestCase):
    """apps.options.tasks.sync_watchlist_option_contracts -- BROKER_MODE guard only (no real broker call in tests)."""

    def test_skips_outside_live_broker_mode(self):
        from django.test import override_settings

        from .tasks import sync_watchlist_option_contracts

        with override_settings(BROKER_MODE="paper"):
            result = sync_watchlist_option_contracts()
        self.assertTrue(result.get("skipped"))
        self.assertIn("BROKER_MODE", result.get("reason", ""))
