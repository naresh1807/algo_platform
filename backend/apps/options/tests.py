from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

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


class SyncWatchlistOptionContractsTests(TestCase):
    """apps.options.tasks.sync_watchlist_option_contracts -- BROKER_MODE guard only (no real broker call in tests)."""

    def test_skips_outside_live_broker_mode(self):
        from django.test import override_settings

        from .tasks import sync_watchlist_option_contracts

        with override_settings(BROKER_MODE="paper"):
            result = sync_watchlist_option_contracts()
        self.assertTrue(result.get("skipped"))
        self.assertIn("BROKER_MODE", result.get("reason", ""))
