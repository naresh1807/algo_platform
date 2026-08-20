"""
apps.options.subscription_manager -- the live-feed subscription scope
fix (fix-list items 1 and 12). Real DB, no broker calls, no Redis
dependency for the parts that don't need it (settings.CACHES falls back
to LocMemCache under TESTING, see config/settings.py).
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.market_data.models import HistoricalData

from .expiry_service import is_expiry_eligible
from .models import OptionContract
from .subscription_manager import (
    chunk_tokens,
    clear_selected_expiry,
    compute_desired_option_tokens,
    desired_tokens_for_underlying,
    get_selected_expiry,
    make_correlation_id,
    resolve_live_expiry,
    set_selected_expiry,
)

UNDERLYING = "TESTIDX"


def _make_contract(expiry, strike, option_type="CE", token_suffix=""):
    return OptionContract.objects.create(
        underlying=UNDERLYING,
        expiry=expiry,
        strike=Decimal(str(strike)),
        option_type=option_type,
        symbol_token=f"tok_{expiry.isoformat()}_{strike}_{option_type}{token_suffix}",
        is_active=is_expiry_eligible(expiry),
    )


def _make_spot(price):
    HistoricalData.objects.create(
        symbol=UNDERLYING, timeframe="1m", timestamp=timezone.now(),
        open=price, high=price, low=price, close=price, volume=0, source="test",
    )


class DesiredTokensTests(TestCase):
    def setUp(self):
        cache.clear()
        self.today = timezone.localdate()
        self.near_expiry = self.today + timedelta(days=7)
        self.far_expiry = self.today + timedelta(days=14)
        self.expired = self.today - timedelta(days=1)

    def tearDown(self):
        clear_selected_expiry(UNDERLYING)

    def test_expired_contracts_are_never_subscribed(self):
        _make_contract(self.expired, 100)
        _make_contract(self.near_expiry, 100)
        _make_spot(100)

        tokens = desired_tokens_for_underlying(UNDERLYING, strike_range=20)

        expired_tokens = [
            meta for meta in tokens.values() if meta["expiry"] == self.expired.isoformat()
        ]
        self.assertEqual(expired_tokens, [])
        self.assertTrue(any(meta["expiry"] == self.near_expiry.isoformat() for meta in tokens.values()))

    def test_current_expiry_selected_by_default(self):
        _make_contract(self.near_expiry, 100)
        _make_contract(self.far_expiry, 100)
        _make_spot(100)

        self.assertEqual(resolve_live_expiry(UNDERLYING), self.near_expiry)

    def test_selected_valid_future_expiry_is_honored(self):
        _make_contract(self.near_expiry, 100)
        _make_contract(self.far_expiry, 100)
        _make_spot(100)

        set_selected_expiry(UNDERLYING, self.far_expiry)

        self.assertEqual(get_selected_expiry(UNDERLYING), self.far_expiry)
        self.assertEqual(resolve_live_expiry(UNDERLYING), self.far_expiry)

    def test_selected_expired_expiry_falls_back_to_current(self):
        _make_contract(self.near_expiry, 100)
        _make_spot(100)

        # Simulates a rollover happening after the operator picked an
        # expiry that has since expired: write the raw cache key
        # directly (bypassing set_selected_expiry's own eligibility
        # assumption at write time) to model "time passed since selection".
        set_selected_expiry(UNDERLYING, self.expired)

        self.assertIsNone(get_selected_expiry(UNDERLYING))
        self.assertEqual(resolve_live_expiry(UNDERLYING), self.near_expiry)

    def test_selected_expiry_with_no_synced_contracts_is_rejected(self):
        _make_contract(self.near_expiry, 100)
        _make_spot(100)

        # A date that is chronologically eligible but was never actually
        # synced for this underlying must never be treated as selected.
        set_selected_expiry(UNDERLYING, self.far_expiry)

        self.assertIsNone(get_selected_expiry(UNDERLYING))
        self.assertEqual(resolve_live_expiry(UNDERLYING), self.near_expiry)

    def test_strike_range_narrows_to_atm_band(self):
        for strike in range(50, 151, 10):  # 50..150 step 10 -> 11 strikes
            _make_contract(self.near_expiry, strike)
        _make_spot(100)  # ATM = 100

        tokens = desired_tokens_for_underlying(UNDERLYING, strike_range=2)
        strikes = sorted({meta["strike"] for meta in tokens.values()})
        # ATM index of 100 is 5 (0-based) in [50,60,...,150]; +/-2 -> strikes 80,90,100,110,120
        self.assertEqual(strikes, [80.0, 90.0, 100.0, 110.0, 120.0])

    def test_strike_range_zero_subscribes_whole_chain(self):
        for strike in range(50, 151, 10):
            _make_contract(self.near_expiry, strike)
        _make_spot(100)

        tokens = desired_tokens_for_underlying(UNDERLYING, strike_range=0)
        self.assertEqual(len(tokens), 11)

    def test_no_spot_price_falls_back_to_whole_expiry_chain(self):
        for strike in range(50, 151, 10):
            _make_contract(self.near_expiry, strike)
        # Deliberately no HistoricalData row -- feed just started.

        tokens = desired_tokens_for_underlying(UNDERLYING, strike_range=2)
        self.assertEqual(len(tokens), 11)

    def test_no_eligible_expiry_returns_empty(self):
        _make_contract(self.expired, 100)
        _make_spot(100)

        self.assertEqual(desired_tokens_for_underlying(UNDERLYING), {})

    def test_compute_desired_option_tokens_merges_underlyings(self):
        _make_contract(self.near_expiry, 100)
        _make_spot(100)
        other = OptionContract.objects.create(
            underlying="OTHERIDX", expiry=self.near_expiry, strike=Decimal("200"),
            option_type="CE", symbol_token="othertok", is_active=True,
        )
        HistoricalData.objects.create(
            symbol="OTHERIDX", timeframe="1m", timestamp=timezone.now(),
            open=200, high=200, low=200, close=200, volume=0, source="test",
        )

        tokens = compute_desired_option_tokens(["TESTIDX", "OTHERIDX"])
        self.assertIn(other.symbol_token, tokens)
        self.assertTrue(any(meta["underlying"] == UNDERLYING for meta in tokens.values()))


class CorrelationIdAndChunkingTests(TestCase):
    def test_correlation_id_is_valid_per_angel_one_spec(self):
        for _ in range(20):
            cid = make_correlation_id("optsub")
            self.assertLessEqual(len(cid), 10)
            self.assertTrue(cid.isalnum())

    def test_correlation_id_strips_non_alnum_prefix(self):
        cid = make_correlation_id("opt_sub!!")
        self.assertTrue(cid.isalnum())
        self.assertLessEqual(len(cid), 10)

    def test_chunk_tokens_splits_into_bounded_batches(self):
        tokens = [str(i) for i in range(125)]
        chunks = chunk_tokens(tokens, chunk_size=50)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([len(c) for c in chunks], [50, 50, 25])
        self.assertEqual([t for chunk in chunks for t in chunk], tokens)

    def test_chunk_tokens_empty_list(self):
        self.assertEqual(chunk_tokens([], chunk_size=50), [])
