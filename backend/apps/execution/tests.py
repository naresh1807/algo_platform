from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.learning.models import StrategyVersion
from apps.risk.models import AccountEquity
from apps.signals.models import TradingSignal
from common.constants import SignalStatus, SignalType

from .models import OpenPosition
from .paper_executor import close_position, open_position_from_signal


class OpenPositionModelTests(TestCase):
    def test_is_open_true_when_no_closed_at(self):
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=100, stop_loss=95,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )
        position = OpenPosition.objects.create(
            signal=signal, symbol="NIFTY", side="long", qty=10, entry_price=100, stop_loss=95,
        )
        self.assertTrue(position.is_open)


class PaperExecutorTests(TestCase):
    def setUp(self):
        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )
        self.signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("100"),
            stop_loss=Decimal("95"), position_size=10,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )

    def test_open_position_marks_signal_executed(self):
        position = open_position_from_signal(self.signal)
        self.signal.refresh_from_db()
        self.assertEqual(self.signal.status, SignalStatus.EXECUTED)
        self.assertEqual(position.qty, 10)

    def test_close_position_updates_equity_on_win(self):
        position = open_position_from_signal(self.signal)
        close_position(position, Decimal("110"), "target hit")

        equity = AccountEquity.objects.get(pk=1)
        # (110 - 100) * 10 qty = 100 profit. The expected value here was
        # previously 101000 (a stale arithmetic error in the test itself
        # -- (110-100)*10 is 100, not 1000; _pnl_for's actual code was
        # always correct). Fixed so this test genuinely guards the real
        # equity-update math instead of asserting a wrong number.
        self.assertEqual(equity.current_equity, Decimal("100100"))
        self.assertEqual(equity.consecutive_losses, 0)

    def test_close_position_increments_consecutive_losses_on_loss(self):
        position = open_position_from_signal(self.signal)
        close_position(position, Decimal("90"), "stop hit")

        equity = AccountEquity.objects.get(pk=1)
        self.assertEqual(equity.consecutive_losses, 1)
        self.assertLess(equity.current_equity, Decimal("100000"))


class TrailingStopTests(TestCase):
    """apps.execution.trailing_stop -- stop only ever ratchets up, never down."""

    def setUp(self):
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("100"),
            stop_loss=Decimal("95"), position_size=10,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )
        self.position = OpenPosition.objects.create(
            signal=signal, symbol="NIFTY", side="long", qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            trailing_stop_distance=Decimal("5"), peak_price=Decimal("100"),
        )

    def test_no_op_when_trailing_not_enabled(self):
        from .trailing_stop import update_trailing_stop

        position = OpenPosition.objects.create(
            signal=self.position.signal, symbol="NIFTY", side="long", qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
        )
        moved = update_trailing_stop(position, Decimal("120"))
        self.assertFalse(moved)
        self.assertEqual(position.stop_loss, Decimal("95"))

    def test_stop_ratchets_up_on_new_high(self):
        from .trailing_stop import update_trailing_stop

        moved = update_trailing_stop(self.position, Decimal("110"))
        self.assertTrue(moved)
        self.position.refresh_from_db()
        self.assertEqual(self.position.stop_loss, Decimal("105"))  # 110 - 5
        self.assertEqual(self.position.peak_price, Decimal("110"))

    def test_stop_never_moves_down_on_pullback(self):
        from .trailing_stop import update_trailing_stop

        update_trailing_stop(self.position, Decimal("110"))  # stop -> 105
        self.position.refresh_from_db()

        moved = update_trailing_stop(self.position, Decimal("102"))  # pulls back
        self.assertFalse(moved)
        self.position.refresh_from_db()
        self.assertEqual(self.position.stop_loss, Decimal("105"))  # unchanged


class WaitForFillTests(TestCase):
    """
    apps.execution.live_executor._wait_for_fill -- specifically the
    cancel-on-timeout safety behavior added during the pre-flight
    review before real Angel One credentials were first used (see
    BrokerClient.cancel_order's own docstring on why this exists: it
    reduces, not eliminates, the risk of an order filling at the
    broker after this codebase has stopped tracking it).

    Uses a fake client (not a real BrokerClient/network call) since
    this needs to run without a broker connection -- only exercises
    _wait_for_fill's own polling/cancel logic.
    """

    def test_returns_immediately_on_complete_status(self):
        from .live_executor import _wait_for_fill

        class FakeClient:
            def get_order_status(self, order_id):
                return {"status": "complete", "orderid": order_id}

        order = _wait_for_fill(FakeClient(), "ORDER123")
        self.assertEqual(order["status"], "complete")

    def test_raises_immediately_on_rejected_status(self):
        from .live_executor import OrderNotFilledError, _wait_for_fill

        class FakeClient:
            def get_order_status(self, order_id):
                return {"status": "rejected", "text": "insufficient margin"}

        with self.assertRaises(OrderNotFilledError):
            _wait_for_fill(FakeClient(), "ORDER123")

    def test_attempts_cancel_on_timeout_and_reports_success(self):
        from . import live_executor
        from .live_executor import OrderNotFilledError, _wait_for_fill

        class FakeClient:
            def __init__(self):
                self.cancel_called_with = None

            def get_order_status(self, order_id):
                return {"status": "open"}  # never fills -> exhausts the poll window

            def cancel_order(self, order_id):
                self.cancel_called_with = order_id
                return True

        original_sleep, original_attempts = live_executor.time.sleep, live_executor.FILL_POLL_MAX_ATTEMPTS
        live_executor.time.sleep = lambda _: None  # don't actually wait in the test
        live_executor.FILL_POLL_MAX_ATTEMPTS = 2
        try:
            client = FakeClient()
            with self.assertRaises(OrderNotFilledError) as ctx:
                _wait_for_fill(client, "ORDER123")
            self.assertEqual(client.cancel_called_with, "ORDER123")
            self.assertIn("cancelled", str(ctx.exception))
        finally:
            live_executor.time.sleep = original_sleep
            live_executor.FILL_POLL_MAX_ATTEMPTS = original_attempts

    def test_warns_loudly_when_cancel_itself_fails(self):
        from . import live_executor
        from .live_executor import OrderNotFilledError, _wait_for_fill

        class FakeClient:
            def get_order_status(self, order_id):
                return {"status": "open"}

            def cancel_order(self, order_id):
                return False  # cancel could not be confirmed

        original_sleep, original_attempts = live_executor.time.sleep, live_executor.FILL_POLL_MAX_ATTEMPTS
        live_executor.time.sleep = lambda _: None
        live_executor.FILL_POLL_MAX_ATTEMPTS = 2
        try:
            with self.assertRaises(OrderNotFilledError) as ctx:
                _wait_for_fill(FakeClient(), "ORDER123")
            self.assertIn("CHECK THE BROKER'S ORDER BOOK MANUALLY", str(ctx.exception))
        finally:
            live_executor.time.sleep = original_sleep
            live_executor.FILL_POLL_MAX_ATTEMPTS = original_attempts
