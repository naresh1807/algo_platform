"""
apps.options.tasks.ingest_option_chain_snapshots -- fix for a REAL,
observed production incident: this task used to query is_active=True
(every non-expired contract across every synced expiry, no strike
narrowing at all) -- 2,116 contracts in one real run, requiring ~43
sequential rate-limited Angel One batch calls. On Celery's --pool=solo,
that blocked the ENTIRE priority queue for minutes, including the
heartbeat task the frontend's "Priority Worker Missing" status depends
on. Now scoped to the exact same set apps.options.subscription_manager
already resolves for the live WebSocket feed (current/selected expiry x
ATM +/- strike range) -- see that task's own updated docstring.

No test existed for this task at all before this incident -- these are
new.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.market_data.models import HistoricalData

from .models import OptionChainSnapshot, OptionContract
from .subscription_manager import clear_selected_expiry

UNDERLYING = "TESTIDX"


def _make_contract(expiry, strike, option_type="CE", token_suffix=""):
    from .expiry_service import is_expiry_eligible

    return OptionContract.objects.create(
        underlying=UNDERLYING, expiry=expiry, strike=Decimal(str(strike)), option_type=option_type,
        symbol_token=f"tok_{expiry.isoformat()}_{strike}_{option_type}{token_suffix}",
        is_active=is_expiry_eligible(expiry),
    )


def _make_spot(price):
    HistoricalData.objects.create(
        symbol=UNDERLYING, timeframe="1m", timestamp=timezone.now(),
        open=price, high=price, low=price, close=price, volume=0, source="test",
    )


class _FakeOptionChainClient:
    """Records exactly which tokens it was asked to quote -- no real broker call."""

    def __init__(self):
        self.requested_tokens: list[str] = []

    def fetch_chain_quotes(self, contracts):
        self.requested_tokens.extend(c["symbol_token"] for c in contracts)
        return [
            {
                "symbol_token": c["symbol_token"], "ltp": 10.0, "open_interest": 100,
                "volume": 5, "iv": None, "bid": 9.5, "ask": 10.5,
            }
            for c in contracts
        ]


@override_settings(BROKER_MODE="live", OPTIONS_PIPELINE_UNDERLYINGS=["TESTIDX"], OPTIONS_LIVE_STRIKE_RANGE=2)
class IngestOptionChainSnapshotsScopeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.today = timezone.localdate()
        self.near_expiry = self.today + timedelta(days=7)
        self.far_expiry = self.today + timedelta(days=45)  # still is_active=True, but far beyond what's live-subscribed

    def tearDown(self):
        cache.clear()
        clear_selected_expiry(UNDERLYING)

    def _run(self, fake_client):
        from .tasks import ingest_option_chain_snapshots
        from unittest.mock import patch

        with patch("apps.options.broker_client.get_option_chain_client", return_value=fake_client):
            return ingest_option_chain_snapshots()

    def test_only_requotes_contracts_within_the_live_subscription_scope(self):
        # Near expiry: strikes 50..150 step 10 (11 strikes), ATM=100 -> with
        # strike_range=2, only 80/90/100/110/120 should be in scope.
        near_tokens = []
        for strike in range(50, 151, 10):
            c = _make_contract(self.near_expiry, strike)
            near_tokens.append(c.symbol_token)
        # Far expiry: also is_active=True (not expired), but far beyond the
        # current/selected expiry -- must NOT be re-quoted by this task.
        far_contract = _make_contract(self.far_expiry, 100)
        _make_spot(100)

        fake_client = _FakeOptionChainClient()
        result = self._run(fake_client)

        self.assertNotIn(far_contract.symbol_token, fake_client.requested_tokens)
        self.assertEqual(len(fake_client.requested_tokens), 5)  # 80,90,100,110,120
        self.assertEqual(OptionChainSnapshot.objects.filter(contract=far_contract).count(), 0)
        self.assertEqual(OptionChainSnapshot.objects.count(), 5)

    def test_expired_contracts_are_never_requoted(self):
        expired = OptionContract.objects.create(
            underlying=UNDERLYING, expiry=self.today - timedelta(days=1), strike=Decimal("100"),
            option_type="CE", symbol_token="tok_expired", is_active=False,
        )
        _make_contract(self.near_expiry, 100)
        _make_spot(100)

        fake_client = _FakeOptionChainClient()
        self._run(fake_client)

        self.assertNotIn("tok_expired", fake_client.requested_tokens)
        self.assertEqual(OptionChainSnapshot.objects.filter(contract=expired).count(), 0)

    def test_no_desired_tokens_skips_without_a_broker_call(self):
        # No contracts synced at all for this underlying.
        fake_client = _FakeOptionChainClient()
        result = self._run(fake_client)

        self.assertEqual(result, {"skipped": True, "reason": "no_contracts_configured"})
        self.assertEqual(fake_client.requested_tokens, [])
