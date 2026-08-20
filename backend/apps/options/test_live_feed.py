"""
apps.options.live_feed -- the fast/slow tick-processing split (fix-list
item 3: "the UI must receive LTP before DB/IV processing"). Verifies the
fast broadcast happens unconditionally and first, survives an IV-solve
failure, and omits fields the fast path cannot know yet (fix-list item
10's "stable identity"/frontend-merge requirement leans on this).
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.market_data.models import HistoricalData

from . import live_feed
from .models import OptionChainSnapshot, OptionContract

UNDERLYING = "TESTIDX"


class HandleOptionTickTests(TestCase):
    def setUp(self):
        live_feed._last_snapshot_at.clear()
        self.expiry = timezone.localdate() + timedelta(days=7)
        self.contract = OptionContract.objects.create(
            underlying=UNDERLYING, expiry=self.expiry, strike=Decimal("100"),
            option_type="CE", symbol_token="tok1", is_active=True,
        )
        self.meta = {
            "contract_id": self.contract.id, "underlying": UNDERLYING,
            "expiry": self.expiry.isoformat(), "strike": 100.0, "option_type": "CE",
        }
        HistoricalData.objects.create(
            symbol=UNDERLYING, timeframe="1m", timestamp=timezone.now(),
            open=100, high=100, low=100, close=100, volume=0, source="test",
        )

    def tearDown(self):
        live_feed._last_snapshot_at.clear()

    @patch("apps.options.candle_aggregator.get_option_candle_aggregator")
    @patch("common.websockets.broadcast_group")
    def test_fast_broadcast_happens_before_snapshot_is_persisted(self, mock_broadcast, mock_aggregator):
        call_order = []
        mock_broadcast.side_effect = lambda *a, **k: call_order.append("broadcast") or True

        original_create = OptionChainSnapshot.objects.create

        def _tracked_create(*args, **kwargs):
            call_order.append("snapshot_persisted")
            return original_create(*args, **kwargs)

        with patch.object(OptionChainSnapshot.objects, "create", side_effect=_tracked_create):
            live_feed.handle_option_tick(self.meta, 12.5, oi=1000, volume=50, bid=12.0, ask=13.0)

        self.assertEqual(call_order, ["broadcast", "snapshot_persisted"])

    @patch("apps.options.candle_aggregator.get_option_candle_aggregator")
    @patch("common.websockets.broadcast_group")
    def test_fast_broadcast_payload_omits_unknown_fields(self, mock_broadcast, mock_aggregator):
        live_feed.handle_option_tick(self.meta, 12.5, oi=1000, volume=50, bid=12.0, ask=13.0)

        self.assertTrue(mock_broadcast.called)
        group, event = mock_broadcast.call_args.args[0], mock_broadcast.call_args.args[1]
        self.assertEqual(group, "options_live")
        data = event["data"]
        self.assertEqual(data["contract_id"], self.contract.id)
        self.assertEqual(data["ltp"], 12.5)
        self.assertNotIn("change_in_oi", data)
        self.assertNotIn("iv", data)
        self.assertNotIn("greeks", data)

    @patch("apps.options.candle_aggregator.get_option_candle_aggregator")
    @patch("apps.options.greeks.implied_volatility", side_effect=RuntimeError("solver blew up"))
    @patch("common.websockets.broadcast_group")
    def test_iv_solver_failure_does_not_block_the_fast_broadcast_or_snapshot(
        self, mock_broadcast, mock_iv, mock_aggregator
    ):
        live_feed.handle_option_tick(self.meta, 12.5, oi=1000, volume=50, bid=12.0, ask=13.0)

        self.assertTrue(mock_broadcast.called)
        snapshot = OptionChainSnapshot.objects.get(contract=self.contract)
        self.assertIsNone(snapshot.iv)
        self.assertEqual(float(snapshot.ltp), 12.5)

    @patch("apps.options.candle_aggregator.get_option_candle_aggregator")
    @patch("common.websockets.broadcast_group")
    def test_snapshot_persistence_is_throttled_but_broadcast_is_not(self, mock_broadcast, mock_aggregator):
        live_feed.handle_option_tick(self.meta, 12.5, oi=1000, volume=50, bid=12.0, ask=13.0)
        live_feed.handle_option_tick(self.meta, 12.6, oi=1001, volume=51, bid=12.1, ask=13.1)

        self.assertEqual(mock_broadcast.call_count, 2)
        self.assertEqual(OptionChainSnapshot.objects.filter(contract=self.contract).count(), 1)

    @patch("apps.options.candle_aggregator.get_option_candle_aggregator")
    @patch("common.websockets.broadcast_group")
    def test_candle_aggregation_receives_correct_contract_id(self, mock_broadcast, mock_aggregator_getter):
        aggregator = mock_aggregator_getter.return_value
        live_feed.handle_option_tick(self.meta, 12.5, oi=1000, volume=50, bid=12.0, ask=13.0)

        aggregator.on_tick.assert_called_once()
        called_contract_id = aggregator.on_tick.call_args.args[0]
        self.assertEqual(called_contract_id, self.contract.id)
