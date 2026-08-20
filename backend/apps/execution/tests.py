from decimal import Decimal

from django.test import TestCase, override_settings
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


@override_settings(
    PAPER_CASH_SLIPPAGE_BPS_PER_SIDE="0",
    PAPER_CASH_FEES_BPS_PER_SIDE="0",
)
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


@override_settings(
    PAPER_CASH_SLIPPAGE_BPS_PER_SIDE="10",
    PAPER_CASH_FEES_BPS_PER_SIDE="5",
)
class PaperExecutorCostPersistenceTests(TestCase):
    def setUp(self):
        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )
        self.signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("100"),
            stop_loss=Decimal("95"), position_size=10,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED, reason="cost test",
        )

    def test_open_snapshots_assumptions_and_adverse_entry_fill(self):
        position = open_position_from_signal(self.signal)

        self.assertEqual(position.entry_reference_price, Decimal("100"))
        self.assertEqual(position.entry_price, Decimal("100.1000"))
        self.assertEqual(position.paper_slippage_bps_per_side, Decimal("10"))
        self.assertEqual(position.paper_fees_bps_per_side, Decimal("5"))
        # Immediate liquidation at the unchanged reference price includes the
        # adverse round trip and both sides' fees.
        self.assertEqual(position.unrealized_pnl, Decimal("-3.00"))

    def test_close_persists_gross_costs_and_net_realized_pnl(self):
        position = open_position_from_signal(self.signal)

        # A deployment setting change after entry must not rewrite this trade's
        # economics; close_position uses the snapshot on `position`.
        with override_settings(
            PAPER_CASH_SLIPPAGE_BPS_PER_SIDE="500",
            PAPER_CASH_FEES_BPS_PER_SIDE="500",
        ):
            close_position(position, Decimal("110"), "target hit")

        position.refresh_from_db()
        equity = AccountEquity.objects.get(pk=1)
        self.assertEqual(position.exit_reference_price, Decimal("110"))
        self.assertEqual(position.exit_price, Decimal("109.8900"))
        self.assertEqual(position.gross_realized_pnl, Decimal("100.00"))
        self.assertEqual(position.slippage_cost, Decimal("2.10"))
        self.assertEqual(position.fees, Decimal("1.05"))
        self.assertEqual(position.total_costs, Decimal("3.15"))
        self.assertEqual(position.realized_pnl, Decimal("96.85"))
        self.assertEqual(position.unrealized_pnl, position.realized_pnl)
        self.assertEqual(equity.current_equity, Decimal("100096.85"))

    @override_settings(TRAILING_STOP_ENABLED=True)
    def test_trailing_risk_is_initialized_from_adverse_entry_fill(self):
        position = open_position_from_signal(self.signal)

        self.assertEqual(position.peak_price, Decimal("100.1000"))
        self.assertEqual(position.trailing_stop_distance, Decimal("5.1000"))

    def test_cost_aware_sizing_keeps_stop_settlement_within_hard_risk_limit(self):
        from .paper_costs import assumptions_from_position, calculate_paper_settlement

        self.signal.position_size = 200
        self.signal.save(update_fields=["position_size"])
        position = open_position_from_signal(self.signal)
        settlement = calculate_paper_settlement(
            entry_reference_price=position.entry_reference_price,
            exit_reference_price=position.stop_loss,
            qty=position.qty,
            side=position.side,
            assumptions=assumptions_from_position(position),
            entry_fill_price=position.entry_price,
        )

        self.assertLess(position.qty, 200)
        self.assertLessEqual(-settlement.net_pnl, Decimal("1000"))

    def test_gapped_stop_uses_observed_price_not_idealized_stop(self):
        from unittest.mock import patch

        from .paper_executor import check_and_close_positions

        position = open_position_from_signal(self.signal)
        with patch(
            "apps.execution.paper_executor.compute_indicators", return_value={"close": 90},
        ):
            results = check_and_close_positions("5m")

        position.refresh_from_db()
        self.assertEqual(results[0]["reason"], "stop_loss")
        self.assertEqual(position.exit_reference_price, Decimal("90"))
        self.assertLess(position.exit_price, Decimal("90"))


@override_settings(
    PAPER_OPTION_SLIPPAGE_BPS_PER_SIDE="0",
    PAPER_OPTION_FEES_BPS_PER_SIDE="0",
)
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

    def test_option_position_snapshots_option_specific_cost_settings(self):
        with override_settings(
            PAPER_OPTION_SLIPPAGE_BPS_PER_SIDE="25",
            PAPER_OPTION_FEES_BPS_PER_SIDE="10",
        ):
            position = open_position_from_signal(self.signal)

        self.assertEqual(position.entry_reference_price, Decimal("110"))
        self.assertEqual(position.entry_price, Decimal("110.2750"))
        self.assertEqual(position.paper_slippage_bps_per_side, Decimal("25"))
        self.assertEqual(position.paper_fees_bps_per_side, Decimal("10"))

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

        position = self.position
        position.trailing_stop_distance = None
        position.peak_price = None
        position.save(update_fields=["trailing_stop_distance", "peak_price"])
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


@override_settings(
    PAPER_CASH_SLIPPAGE_BPS_PER_SIDE="0",
    PAPER_CASH_FEES_BPS_PER_SIDE="0",
)
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
        from unittest.mock import patch

        # Keep the execution-cycle test deterministic: it is about routing
        # an approved SELL signal to the paper executor, not the wall clock,
        # exchange calendar, or feed-health state of the machine running it.
        with patch(
            "apps.market_data.market_hours.is_market_open", return_value=(True, "open"),
        ), patch(
            "apps.risk.engine.validate_signal_for_execution", return_value=(True, ""),
        ), patch(
            "apps.execution.tasks.enforce_account_risk_limits", return_value=(True, ""),
        ), patch(
            "apps.execution.tasks.is_kill_switch_active", return_value=False,
        ):
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
                return {
                    "status": "complete", "orderid": order_id,
                    "averageprice": "100", "filledshares": "1",
                }

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
                self.cancelled = False

            def get_order_status(self, order_id):
                if self.cancelled:
                    return {"status": "cancelled", "filledshares": "0"}
                return {"status": "open"}  # never fills -> exhausts the poll window

            def cancel_order(self, order_id):
                self.cancel_called_with = order_id
                self.cancelled = True
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

    def test_cancelled_order_with_a_partial_fill_returns_the_fill(self):
        from .live_executor import _wait_for_fill

        class FakeClient:
            def get_order_status(self, order_id):
                return {
                    "status": "cancelled", "orderid": order_id,
                    "averageprice": "101.25", "filledshares": "10",
                }

        order = _wait_for_fill(FakeClient(), "ORDER123")
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order["filledshares"], "10")

    def test_complete_order_with_invalid_fill_data_is_unknown(self):
        from .live_executor import OrderStateUnknownError, _wait_for_fill

        class FakeClient:
            def get_order_status(self, order_id):
                return {
                    "status": "complete", "orderid": order_id,
                    "averageprice": "0", "filledshares": "1",
                }

        with self.assertRaises(OrderStateUnknownError):
            _wait_for_fill(FakeClient(), "ORDER123")

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


class LiveExecutionSafetyGateTests(TestCase):
    """The deployment arming gate must fail before any broker I/O."""

    def setUp(self):
        from .models import ExecutionModeSetting

        ExecutionModeSetting.objects.create(pk=1, mode=ExecutionModeSetting.Mode.LIVE)
        self.signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY,
            entry_price=Decimal("100"), stop_loss=Decimal("95"), position_size=1,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED,
            execution_mode=ExecutionModeSetting.Mode.LIVE, reason="gate test",
        )

    @override_settings(LIVE_TRADING_ENABLED=False)
    def test_direct_live_open_is_disarmed_before_broker_client_is_created(self):
        from unittest.mock import patch

        from .live_executor import open_position_live

        with patch("apps.execution.live_executor.get_broker_client") as get_client:
            with self.assertRaisesRegex(PermissionError, "disarmed"):
                open_position_live(self.signal)

        get_client.assert_not_called()
        self.assertFalse(self.signal.broker_orders.exists())

    @override_settings(LIVE_TRADING_ENABLED=False)
    def test_live_cycle_is_disarmed_before_reconciliation_or_submission(self):
        """
        run_trading_cycle() no longer short-circuits to a bare
        {"skipped": True} for a disarmed live deployment with no
        existing broker exposure (see that function's own "disarming is
        an entry gate, not permission to abandon exposure that already
        exists" comment) -- it still runs the full cycle and reports
        the block via skipped_exposure, opening nothing and making no
        broker call, which is what this test actually needs to verify.
        """
        from .tasks import run_trading_cycle

        result = run_trading_cycle()

        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["opened"], [])
        self.assertEqual(result["skipped_exposure"], [{"reason": "live_trading_disarmed"}])
        self.assertEqual(result["equity_sync"], {"synced": False, "reason": "live_trading_disarmed"})
        self.assertFalse(self.signal.broker_orders.exists())


@override_settings(LIVE_TRADING_ENABLED=True)
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

        from .models import ExecutionModeSetting

        ExecutionModeSetting.objects.create(
            pk=1, mode=ExecutionModeSetting.Mode.LIVE,
        )

        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
            source_mode=ExecutionModeSetting.Mode.LIVE,
            last_broker_sync_at=timezone.now(),
        )
        self.contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=timezone.localdate() + timedelta(days=7),
            strike=24400, option_type="PE", symbol_token="tok_pe_24400",
            tradingsymbol="NIFTY24400PE", lot_size=25,
        )
        # signal_type is deliberately BUY even though option_side is PE (a
        # bearish view on the underlying): apps.execution.live_executor.
        # _validate_live_signal_integrity requires every live OPTION order
        # to be an explicit BUY signal (buying the PE's own premium) --
        # a live "SELL" signal_type reaching real order placement would be
        # ambiguous with sell-to-open/naked-writing, which this platform
        # must never do automatically. See that function's own check.
        self.signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("110"),
            stop_loss=Decimal("95"), target_1=Decimal("140"), position_size=25,
            option_side="PE", strike_price=Decimal("24400"), option_contract=self.contract,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED,
            execution_mode=ExecutionModeSetting.Mode.LIVE, reason="test",
        )

    def _fake_client(self):
        class FakeClient:
            def __init__(self):
                self.place_order_calls = []
                self.next_order_number = 1

            def place_order(self, symbol, transaction_type, qty, order_type="MARKET", price=0.0,
                             symbol_token=None, exchange=None, tradingsymbol=None,
                             order_tag=None, risk_reducing=False):
                self.place_order_calls.append({
                    "symbol": symbol, "transaction_type": transaction_type, "qty": qty,
                    "symbol_token": symbol_token, "exchange": exchange, "tradingsymbol": tradingsymbol,
                    "order_tag": order_tag, "risk_reducing": risk_reducing,
                })
                order_id = f"ORDER{self.next_order_number}"
                self.next_order_number += 1
                return order_id

            def get_order_status(self, order_id):
                return {"status": "complete", "averageprice": "112.5", "filledshares": "25"}

            def get_positions(self):
                # apps.execution.live_executor._verify_broker_exposure_before_exit
                # confirms the broker's own reported position still
                # matches what we expect LOCALLY before submitting any
                # exit order (a real safety check against a desynced/
                # already-closed broker position) -- matches this class's
                # own contract/signal fixture (self.contract.tradingsymbol
                # = "NIFTY24400PE", self.signal.position_size = 25, a full
                # fill per this fake's own get_order_status above).
                return [{"tradingsymbol": "NIFTY24400PE", "netqty": 25, "producttype": "INTRADAY"}]

        return FakeClient()

    def test_open_position_live_places_nfo_order_on_the_contract(self):
        from unittest.mock import patch

        from .live_executor import open_position_live

        client = self._fake_client()
        with patch("apps.execution.live_executor.get_broker_client", return_value=client), \
             patch("apps.execution.live_executor.validate_signal_for_execution", return_value=(True, "")):
            position = open_position_live(self.signal)

        self.assertEqual(len(client.place_order_calls), 1)
        call = client.place_order_calls[0]
        self.assertEqual(call["symbol"], "NIFTY24400PE")
        self.assertEqual(call["symbol_token"], "tok_pe_24400")
        self.assertEqual(call["exchange"], "NFO")
        self.assertEqual(call["tradingsymbol"], "NIFTY24400PE")
        self.assertTrue(call["order_tag"].startswith("ap"))
        # A BUY signal on a PE contract still opens LONG -- buying a PUT is
        # a long bet on the PUT's own premium, LONG regardless of whether
        # the contract itself represents a bullish (CE) or bearish (PE)
        # view on the underlying.
        self.assertEqual(position.side, PositionSide.LONG)
        self.assertEqual(position.symbol, "NIFTY24400PE")
        self.assertEqual(position.option_contract_id, self.contract.pk)

    def test_open_position_live_refuses_plain_index_without_calling_broker(self):
        from unittest.mock import patch

        from .live_executor import open_position_live

        plain_signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("24500"),
            stop_loss=Decimal("24400"), position_size=10,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.APPROVED,
            execution_mode="live", reason="test",
        )
        client = self._fake_client()
        with patch("apps.execution.live_executor.get_broker_client", return_value=client) as get_client:
            with self.assertRaisesRegex(ValueError, "no resolved tradable contract"):
                open_position_live(plain_signal)

        get_client.assert_not_called()
        self.assertEqual(client.place_order_calls, [])
        plain_signal.refresh_from_db()
        self.assertEqual(plain_signal.status, SignalStatus.REJECTED)

    def test_partial_open_fill_tracks_only_the_quantity_the_broker_filled(self):
        from unittest.mock import patch

        from .live_executor import open_position_live
        from .models import BrokerOrder

        client = self._fake_client()
        client.get_order_status = lambda order_id: {
            "status": "cancelled", "orderid": order_id,
            "averageprice": "111.5", "filledshares": "10",
        }
        with patch("apps.execution.live_executor.get_broker_client", return_value=client), \
             patch("apps.execution.live_executor.validate_signal_for_execution", return_value=(True, "")):
            position = open_position_live(self.signal)

        journal = BrokerOrder.objects.get(signal=self.signal, purpose=BrokerOrder.Purpose.OPEN)
        self.assertEqual(position.qty, 10)
        self.assertEqual(journal.status, BrokerOrder.Status.FILLED)
        self.assertEqual(journal.filled_quantity, 10)
        self.assertIn("10/25", journal.error)

    def test_partial_close_fill_leaves_position_open_and_order_unknown(self):
        from unittest.mock import patch

        from .live_executor import OrderStateUnknownError, close_position_live, open_position_live
        from .models import BrokerOrder

        client = self._fake_client()
        with patch("apps.execution.live_executor.get_broker_client", return_value=client), \
             patch("apps.execution.live_executor.validate_signal_for_execution", return_value=(True, "")):
            position = open_position_live(self.signal)

        client.get_order_status = lambda order_id: {
            "status": "cancelled", "orderid": order_id,
            "averageprice": "115", "filledshares": "10",
        }
        with patch("apps.execution.live_executor.get_broker_client", return_value=client):
            with self.assertRaisesRegex(OrderStateUnknownError, "partial position reconciliation"):
                close_position_live(position, "test partial exit")

        position.refresh_from_db()
        self.assertIsNone(position.closed_at)
        close_order = BrokerOrder.objects.get(
            position=position, purpose=BrokerOrder.Purpose.CLOSE,
        )
        self.assertEqual(close_order.status, BrokerOrder.Status.UNKNOWN)
        self.assertEqual(AccountEquity.objects.get(pk=1).current_equity, Decimal("100000"))

    def test_unknown_submission_is_not_placed_again(self):
        from unittest.mock import patch

        from . import live_executor
        from .live_executor import OrderStateUnknownError, open_position_live
        from .models import BrokerOrder

        client = self._fake_client()
        client.get_order_status = lambda order_id: {"status": "open", "filledshares": "0"}
        client.cancel_order = lambda order_id: False
        with patch("apps.execution.live_executor.get_broker_client", return_value=client), \
             patch("apps.execution.live_executor.validate_signal_for_execution", return_value=(True, "")), \
             patch.object(live_executor, "FILL_POLL_MAX_ATTEMPTS", 1), \
             patch.object(live_executor.time, "sleep", return_value=None):
            with self.assertRaises(OrderStateUnknownError):
                open_position_live(self.signal)
            with self.assertRaisesRegex(ValueError, "not executable"):
                open_position_live(self.signal)

        self.assertEqual(len(client.place_order_calls), 1)
        self.signal.refresh_from_db()
        self.assertEqual(self.signal.status, SignalStatus.EXECUTING)
        self.assertEqual(
            BrokerOrder.objects.filter(signal=self.signal, status=BrokerOrder.Status.UNKNOWN).count(),
            1,
        )

    def test_check_and_close_positions_live_uses_contract_quote_and_skips_technical_exit(self):
        from unittest.mock import patch

        from .live_executor import check_and_close_positions_live, open_position_live

        client = self._fake_client()
        with patch("apps.execution.live_executor.get_broker_client", return_value=client), \
             patch("apps.execution.live_executor.validate_signal_for_execution", return_value=(True, "")):
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
        with patch("apps.execution.live_executor.get_broker_client", return_value=client), \
             patch("apps.execution.live_executor.validate_signal_for_execution", return_value=(True, "")):
            position = open_position_live(self.signal)

        with patch("apps.execution.live_executor.get_broker_client", return_value=client), \
             patch("apps.options.pricing.latest_ltp_for_contract", return_value=None):
            results = check_and_close_positions_live("5m")

        self.assertEqual(results, [])
        position.refresh_from_db()
        self.assertIsNone(position.closed_at)


class ExecutionVenueAndTimeframeIsolationTests(TestCase):
    """Exit scans must never cross either the venue or timeframe boundary."""

    def _position(self, symbol, execution_mode, timeframe):
        signal = TradingSignal.objects.create(
            symbol=symbol, signal_type=SignalType.BUY,
            entry_price=Decimal("100"), stop_loss=Decimal("90"), target_1=Decimal("120"),
            position_size=1, total_score=1, technical_score=1,
            sentiment_score=0, risk_score=1, regime="trending",
            status=SignalStatus.EXECUTED, execution_mode=execution_mode,
            reason="isolation test",
        )
        return OpenPosition.objects.create(
            signal=signal, symbol=symbol, execution_mode=execution_mode,
            timeframe=timeframe, side=PositionSide.LONG, qty=1,
            entry_price=Decimal("100"), stop_loss=Decimal("90"),
            target_price=Decimal("120"),
        )

    def test_paper_exit_scan_only_marks_matching_paper_timeframe(self):
        from unittest.mock import patch

        from .paper_executor import check_and_close_positions

        matching = self._position("PAPER5", "paper", "5m")
        other_timeframe = self._position("PAPER1", "paper", "1m")
        other_venue = self._position("LIVE5", "live", "5m")

        with patch(
            "apps.execution.paper_executor.compute_indicators", return_value={"close": 101},
        ), patch(
            "apps.execution.paper_executor.should_exit_position", return_value=(False, []),
        ):
            results = check_and_close_positions("5m")

        self.assertEqual([row["symbol"] for row in results], ["PAPER5"])
        for position in (matching, other_timeframe, other_venue):
            position.refresh_from_db()
        self.assertEqual(matching.unrealized_pnl, Decimal("1"))
        self.assertEqual(other_timeframe.unrealized_pnl, Decimal("0"))
        self.assertEqual(other_venue.unrealized_pnl, Decimal("0"))

    def test_live_exit_scan_only_marks_matching_live_timeframe(self):
        from unittest.mock import patch

        from .live_executor import check_and_close_positions_live

        matching = self._position("LIVE5", "live", "5m")
        other_timeframe = self._position("LIVE1", "live", "1m")
        other_venue = self._position("PAPER5", "paper", "5m")

        with patch(
            "apps.market_data.indicators.compute_indicators", return_value={"close": 101},
        ), patch(
            "apps.signals.engine.should_exit_position", return_value=(False, []),
        ):
            results = check_and_close_positions_live("5m")

        self.assertEqual([row["symbol"] for row in results], ["LIVE5"])
        for position in (matching, other_timeframe, other_venue):
            position.refresh_from_db()
        self.assertEqual(matching.unrealized_pnl, Decimal("1"))
        self.assertEqual(other_timeframe.unrealized_pnl, Decimal("0"))
        self.assertEqual(other_venue.unrealized_pnl, Decimal("0"))


class TradingCycleSignalIsolationTests(TestCase):
    def test_cycle_expires_stale_signal_and_only_routes_current_mode(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch

        from .models import ExecutionModeSetting
        from .tasks import run_trading_cycle

        ExecutionModeSetting.objects.create(pk=1, mode=ExecutionModeSetting.Mode.PAPER)
        fresh_paper = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=100,
            stop_loss=95, position_size=1, total_score=1, technical_score=1,
            sentiment_score=0, risk_score=1, regime="trending",
            status=SignalStatus.APPROVED, execution_mode="paper", reason="fresh paper",
        )
        live_signal = TradingSignal.objects.create(
            symbol="BANKNIFTY", signal_type=SignalType.BUY, entry_price=100,
            stop_loss=95, position_size=1, total_score=1, technical_score=1,
            sentiment_score=0, risk_score=1, regime="trending",
            status=SignalStatus.APPROVED, execution_mode="live", reason="fresh live",
        )
        stale = TradingSignal.objects.create(
            symbol="FINNIFTY", signal_type=SignalType.BUY, entry_price=100,
            stop_loss=95, position_size=1, total_score=1, technical_score=1,
            sentiment_score=0, risk_score=1, regime="trending",
            status=SignalStatus.APPROVED, execution_mode="paper", reason="stale paper",
        )
        TradingSignal.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(minutes=30),
        )

        with patch(
            "apps.market_data.market_hours.is_market_open", return_value=(True, "open"),
        ), patch(
            "apps.execution.tasks.enforce_account_risk_limits", return_value=(True, ""),
        ), patch(
            "apps.execution.tasks.is_kill_switch_active", return_value=False,
        ), patch(
            "apps.execution.paper_executor.check_and_close_positions", return_value=[],
        ), patch(
            "apps.execution.paper_executor.open_position_from_signal",
            return_value=SimpleNamespace(pk=123),
        ) as opener:
            result = run_trading_cycle("5m")

        opener.assert_called_once()
        self.assertEqual(opener.call_args.args[0].pk, fresh_paper.pk)
        self.assertEqual(opener.call_args.kwargs["timeframe"], "5m")
        self.assertTrue(opener.call_args.kwargs["enforce_execution_risk"])
        self.assertEqual(result["opened"], [{"symbol": "NIFTY", "position_id": 123}])
        stale.refresh_from_db()
        live_signal.refresh_from_db()
        self.assertEqual(stale.status, SignalStatus.EXPIRED)
        self.assertIn("expired before execution", stale.reason)
        self.assertEqual(live_signal.status, SignalStatus.APPROVED)
