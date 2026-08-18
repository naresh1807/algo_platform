from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.learning.models import StrategyVersion
from apps.risk.models import AccountEquity
from apps.signals.models import TradingSignal
from common.constants import PositionSide, SignalStatus, SignalType

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


class PaperExecutorOptionContractTests(TestCase):
    """
    apps.execution.paper_executor's real-option-order path -- a
    TradingSignal with option_contract set (apps.options.
    index_direction_strategy's premium-based execution, see that
    module's docstring) must open a LONG OpenPosition priced at the
    CONTRACT's own tradingsymbol/premium, regardless of the signal's own
    signal_type/symbol (the underlying), and must close on stop/target
    using a live contract quote instead of compute_indicators.
    """

    def setUp(self):
        from datetime import timedelta

        from apps.options.models import OptionContract

        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )
        self.contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=timezone.localdate() + timedelta(days=7),
            strike=24400, option_type="PE", symbol_token="tok_pe_24400",
            tradingsymbol="NIFTY24400PE", lot_size=25,
        )
        # signal_type SELL (as the underlying-direction "down" case would
        # produce) but option_contract set -- the position must still
        # open LONG (buying a PE is a long bet on the PE's own premium).
        self.signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.SELL, entry_price=Decimal("110"),
            stop_loss=Decimal("95"), target_1=Decimal("140"), position_size=25,
            option_side="PE", strike_price=Decimal("24400"), option_contract=self.contract,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )

    def test_open_position_is_long_on_the_contract_tradingsymbol(self):
        position = open_position_from_signal(self.signal)
        self.assertEqual(position.side, PositionSide.LONG)
        self.assertEqual(position.symbol, "NIFTY24400PE")
        self.assertEqual(position.option_contract_id, self.contract.pk)
        self.assertEqual(position.entry_price, Decimal("110"))

    def test_check_and_close_positions_uses_live_contract_quote(self):
        from unittest.mock import patch

        from .paper_executor import check_and_close_positions

        open_position_from_signal(self.signal)

        with patch("apps.options.pricing.latest_ltp_for_contract", return_value=145.0):
            results = check_and_close_positions("5m")

        self.assertEqual(results[0]["reason"], "target")
        equity = AccountEquity.objects.get(pk=1)
        # (140 target - 110 entry) * 25 qty = 750 profit, realized at the
        # signal's own target_1 price (close_position closes at
        # position.target_price, not the live quote that triggered it).
        self.assertEqual(equity.current_equity, Decimal("100750"))

    def test_check_and_close_positions_skips_when_quote_unavailable(self):
        from unittest.mock import patch

        from .paper_executor import check_and_close_positions

        position = open_position_from_signal(self.signal)

        with patch("apps.options.pricing.latest_ltp_for_contract", return_value=None):
            results = check_and_close_positions("5m")

        self.assertEqual(results, [])
        position.refresh_from_db()
        self.assertIsNone(position.closed_at)


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


class PaperExecutorShortTests(TestCase):
    """SELL signals open SHORT positions with mirrored P&L (profit as price falls)."""

    def setUp(self):
        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )
        self.signal = TradingSignal.objects.create(
            symbol="BANKNIFTY", signal_type=SignalType.SELL, entry_price=Decimal("100"),
            stop_loss=Decimal("105"), position_size=10,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )

    def test_open_position_creates_short_side(self):
        position = open_position_from_signal(self.signal)
        self.assertEqual(position.side, PositionSide.SHORT)

    def test_close_position_profits_when_price_falls(self):
        position = open_position_from_signal(self.signal)
        close_position(position, Decimal("90"), "target hit")

        equity = AccountEquity.objects.get(pk=1)
        # SHORT P&L is (entry - exit) * qty: (100 - 90) * 10 = 100 profit.
        self.assertEqual(equity.current_equity, Decimal("100100"))
        self.assertEqual(equity.consecutive_losses, 0)

    def test_close_position_loses_when_price_rises(self):
        position = open_position_from_signal(self.signal)
        close_position(position, Decimal("110"), "stop hit")

        equity = AccountEquity.objects.get(pk=1)
        self.assertEqual(equity.consecutive_losses, 1)
        self.assertLess(equity.current_equity, Decimal("100000"))


class CheckAndClosePositionsShortTests(TestCase):
    """
    apps.execution.paper_executor.check_and_close_positions -- a SHORT's
    stop sits ABOVE entry and target sits BELOW, the opposite of LONG,
    so the hit-comparisons must flip by side.
    """

    def setUp(self):
        from datetime import timedelta

        from apps.market_data.models import HistoricalData

        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )
        self.signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.SELL, entry_price=Decimal("100"),
            stop_loss=Decimal("105"), target_1=Decimal("90"), position_size=10,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )
        self.position = OpenPosition.objects.create(
            signal=self.signal, symbol="NIFTY", side=PositionSide.SHORT, qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("105"), target_price=Decimal("90"),
        )

        now = timezone.now()
        for i in range(61, 0, -1):
            close = 110 if i == 1 else 100 + (i % 3)  # latest candle closes at 110
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="5m", timestamp=now - timedelta(minutes=5 * i),
                open=close, high=close + 1, low=close - 1, close=close,
                volume=100000, source="test",
            )

    def test_stop_hit_when_price_rises_above_short_stop(self):
        from .paper_executor import check_and_close_positions

        results = check_and_close_positions("5m")
        self.position.refresh_from_db()
        self.assertIsNotNone(self.position.closed_at)
        self.assertEqual(results[0]["reason"], "stop_loss")


class TrailingStopShortTests(TestCase):
    """apps.execution.trailing_stop -- SHORT's stop only ever ratchets down, never up."""

    def setUp(self):
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.SELL, entry_price=Decimal("100"),
            stop_loss=Decimal("105"), position_size=10,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )
        self.position = OpenPosition.objects.create(
            signal=signal, symbol="NIFTY", side=PositionSide.SHORT, qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("105"),
            trailing_stop_distance=Decimal("5"), peak_price=Decimal("100"),
        )

    def test_stop_ratchets_down_on_new_low(self):
        from .trailing_stop import update_trailing_stop

        moved = update_trailing_stop(self.position, Decimal("90"))
        self.assertTrue(moved)
        self.position.refresh_from_db()
        self.assertEqual(self.position.stop_loss, Decimal("95"))  # 90 + 5
        self.assertEqual(self.position.peak_price, Decimal("90"))

    def test_stop_never_moves_up_on_bounce(self):
        from .trailing_stop import update_trailing_stop

        update_trailing_stop(self.position, Decimal("90"))  # stop -> 95
        self.position.refresh_from_db()

        moved = update_trailing_stop(self.position, Decimal("98"))  # bounces back up
        self.assertFalse(moved)
        self.position.refresh_from_db()
        self.assertEqual(self.position.stop_loss, Decimal("95"))  # unchanged


class RunTradingCycleShortTests(TestCase):
    """apps.execution.tasks.run_trading_cycle now also picks up APPROVED SELL signals."""

    def test_sell_signal_opens_a_short_position(self):
        from .models import ExecutionModeSetting
        from .tasks import run_trading_cycle

        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )
        signal = TradingSignal.objects.create(
            symbol="BANKNIFTY", signal_type=SignalType.SELL, entry_price=Decimal("100"),
            stop_loss=Decimal("105"), position_size=10,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )

        # Explicit, not just relying on ExecutionModeSetting's own
        # "paper" default -- run_trading_cycle now reads this DB row
        # (apps.execution.models.get_execution_mode), not
        # settings.EXECUTION_MODE, so override_settings no longer has
        # any effect on it.
        ExecutionModeSetting.objects.create(pk=1, mode=ExecutionModeSetting.Mode.PAPER)
        run_trading_cycle()

        signal.refresh_from_db()
        self.assertEqual(signal.status, SignalStatus.EXECUTED)
        self.assertEqual(
            OpenPosition.objects.filter(symbol="BANKNIFTY", side=PositionSide.SHORT).count(), 1,
        )


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


class LiveExecutorOptionContractTests(TestCase):
    """
    apps.execution.live_executor's real-option-order path -- mirrors
    PaperExecutorOptionContractTests above, but for live_executor: a
    TradingSignal with option_contract set must place a real NFO order
    using the CONTRACT's own symbol_token/tradingsymbol (via
    BrokerClient.place_order's override kwargs), not look `signal.symbol`
    (the underlying) up in SYMBOL_TOKENS, and check_and_close_positions_live
    must price/exit it the same option-aware way paper_executor does.

    Uses a fake broker client (no real network/Angel One session) that
    records exactly what place_order was called with.
    """

    def setUp(self):
        from datetime import timedelta

        from apps.options.models import OptionContract

        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )
        self.contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=timezone.localdate() + timedelta(days=7),
            strike=24400, option_type="PE", symbol_token="tok_pe_24400",
            tradingsymbol="NIFTY24400PE", lot_size=25,
        )
        self.signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.SELL, entry_price=Decimal("110"),
            stop_loss=Decimal("95"), target_1=Decimal("140"), position_size=25,
            option_side="PE", strike_price=Decimal("24400"), option_contract=self.contract,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )

    def _fake_client(self):
        class FakeClient:
            def __init__(self):
                self.place_order_calls = []

            def place_order(self, symbol, transaction_type, qty, order_type="MARKET", price=0.0,
                             symbol_token=None, exchange=None, tradingsymbol=None):
                self.place_order_calls.append({
                    "symbol": symbol, "transaction_type": transaction_type, "qty": qty,
                    "symbol_token": symbol_token, "exchange": exchange, "tradingsymbol": tradingsymbol,
                })
                return "ORDER1"

            def get_order_status(self, order_id):
                return {"status": "complete", "averageprice": "112.5", "filledshares": "25"}

        return FakeClient()

    def test_open_position_live_places_nfo_order_on_the_contract(self):
        from unittest.mock import patch

        from .live_executor import open_position_live

        client = self._fake_client()
        with patch("apps.execution.live_executor.get_broker_client", return_value=client):
            position = open_position_live(self.signal)

        self.assertEqual(len(client.place_order_calls), 1)
        call = client.place_order_calls[0]
        self.assertEqual(call["symbol"], "NIFTY24400PE")
        self.assertEqual(call["symbol_token"], "tok_pe_24400")
        self.assertEqual(call["exchange"], "NFO")
        self.assertEqual(call["tradingsymbol"], "NIFTY24400PE")
        # SELL signal_type but option_contract set -- must still open LONG
        # (buying a PE is a long bet on the PE's own premium).
        self.assertEqual(position.side, PositionSide.LONG)
        self.assertEqual(position.symbol, "NIFTY24400PE")
        self.assertEqual(position.option_contract_id, self.contract.pk)

    def test_open_position_live_plain_symbol_does_not_pass_option_overrides(self):
        from unittest.mock import patch

        from .live_executor import open_position_live

        plain_signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("24500"),
            stop_loss=Decimal("24400"), position_size=10,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )
        client = self._fake_client()
        with patch("apps.execution.live_executor.get_broker_client", return_value=client):
            open_position_live(plain_signal)

        call = client.place_order_calls[0]
        self.assertEqual(call["symbol"], "NIFTY")
        self.assertIsNone(call["symbol_token"])
        self.assertIsNone(call["exchange"])
        self.assertIsNone(call["tradingsymbol"])

    def test_check_and_close_positions_live_uses_contract_quote_and_skips_technical_exit(self):
        from unittest.mock import patch

        from .live_executor import check_and_close_positions_live, open_position_live

        client = self._fake_client()
        with patch("apps.execution.live_executor.get_broker_client", return_value=client):
            open_position_live(self.signal)

        # should_exit_position must never even be consulted for an option
        # position -- patched to raise if called, so the test fails loudly
        # if that branch is ever reached for this position.
        def _should_not_be_called(*args, **kwargs):
            raise AssertionError("should_exit_position must be skipped for option positions")

        with patch("apps.execution.live_executor.get_broker_client", return_value=client), \
             patch("apps.options.pricing.latest_ltp_for_contract", return_value=145.0), \
             patch("apps.signals.engine.should_exit_position", side_effect=_should_not_be_called):
            results = check_and_close_positions_live("5m")

        self.assertEqual(results[0]["reason"], "target")
        # The closing order must ALSO carry the contract's own token/exchange.
        closing_call = client.place_order_calls[-1]
        self.assertEqual(closing_call["symbol_token"], "tok_pe_24400")
        self.assertEqual(closing_call["exchange"], "NFO")

    def test_check_and_close_positions_live_skips_when_quote_unavailable(self):
        from unittest.mock import patch

        from .live_executor import check_and_close_positions_live, open_position_live

        client = self._fake_client()
        with patch("apps.execution.live_executor.get_broker_client", return_value=client):
            position = open_position_live(self.signal)

        with patch("apps.execution.live_executor.get_broker_client", return_value=client), \
             patch("apps.options.pricing.latest_ltp_for_contract", return_value=None):
            results = check_and_close_positions_live("5m")

        self.assertEqual(results, [])
        position.refresh_from_db()
        self.assertIsNone(position.closed_at)
