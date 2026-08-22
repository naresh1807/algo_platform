"""
Scenarios 19-23: slippage/spread affect fill prices, charges affect net
P&L, stop-loss and target exits work, trailing stop works, intraday
square-off works.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from . import tasks, test_support
from .models import PaperAccount, get_or_create_account
from .services import execution_engine, market_data_reader, position_service


class ExecutionRealismTests(TestCase):
    def setUp(self):
        test_support.seed_charge_rules()
        self.account = get_or_create_account()
        self.contracts, self.expiry = test_support.seed_option_chain()
        self.contract = self.contracts[(list(self.contracts.keys())[0][0], "CE")]
        self.market_open_patch = patch("apps.market_data.market_hours.is_market_open", return_value=(True, ""))
        self.market_open_patch.start()
        self.addCleanup(self.market_open_patch.stop)

    def test_slippage_and_spread_affect_fill_price_vs_ltp(self):
        quote = market_data_reader.latest_contract_quote(self.contract)
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        # Buying must fill AT OR ABOVE the ask (never optimistically at LTP).
        self.assertGreaterEqual(order.average_fill_price, Decimal(str(quote.ask)))
        self.assertNotEqual(order.average_fill_price, Decimal(str(quote.ltp)))

    @override_settings(AI_PAPER_SLIPPAGE_BPS=500)  # 5% -- exaggerated so the effect is unmistakable
    def test_higher_slippage_setting_produces_a_worse_fill_price(self):
        quote = market_data_reader.latest_contract_quote(self.contract)
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        expected_min = Decimal(str(quote.ask)) * Decimal("1.04")  # allow for tick rounding
        self.assertGreater(order.average_fill_price, expected_min)

    def test_charges_reduce_net_pnl_below_gross_pnl_on_a_winning_trade(self):
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position = position_service.open_position(self.account, self.contract, order, stop_loss=Decimal("0.01"), target_price=None)
        # push price up for a winning exit
        from apps.options.models import OptionChainSnapshot
        higher = position.average_entry_price + Decimal("20")
        OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=timezone.now(), ltp=higher, open_interest=5000,
            change_in_oi=0, volume=2000, iv=Decimal("15"), bid=higher - Decimal("0.5"), ask=higher + Decimal("0.5"),
        )
        trade = position_service.close_position(self.account, position, "manual_test_win")
        self.assertGreater(trade.gross_pnl, trade.net_pnl)
        self.assertGreater(trade.brokerage + trade.stt + trade.exchange_charges + trade.gst + trade.sebi_charges + trade.stamp_duty, 0)

    def test_stop_loss_and_target_exits_work(self):
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position = position_service.open_position(
            self.account, self.contract, order,
            stop_loss=order.average_fill_price - Decimal("5"),
            target_price=order.average_fill_price + Decimal("5"),
        )
        from apps.options.models import OptionChainSnapshot
        target_price = position.target_price + Decimal("1")
        OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=timezone.now(), ltp=target_price, open_interest=5000,
            change_in_oi=0, volume=2000, iv=Decimal("15"), bid=target_price - Decimal("0.5"), ask=target_price + Decimal("0.5"),
        )
        from .services import exit_manager

        quote = market_data_reader.latest_contract_quote(self.contract)
        reason, _ = exit_manager.evaluate_hard_exit(position, quote)
        self.assertEqual(reason, "target")

    def test_trailing_stop_ratchets_up_and_never_down(self):
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position = position_service.open_position(
            self.account, self.contract, order, stop_loss=order.average_fill_price - Decimal("10"), target_price=None,
            trailing_stop_distance=Decimal("5"),
        )
        original_stop = position.stop_loss
        moved = position_service.apply_trailing_stop(position, position.average_entry_price + Decimal("20"))
        self.assertIsNotNone(moved)
        self.assertGreater(position.stop_loss, original_stop)

        stop_after_first_move = position.stop_loss
        # A LOWER mark price must never pull the stop back down.
        no_move = position_service.apply_trailing_stop(position, position.average_entry_price + Decimal("2"))
        self.assertIsNone(no_move)
        self.assertEqual(position.stop_loss, stop_after_first_move)

    def test_tighten_stop_refuses_to_widen(self):
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position = position_service.open_position(self.account, self.contract, order, stop_loss=Decimal("100"), target_price=None)
        with self.assertRaises(PermissionError):
            position_service.tighten_stop(position, Decimal("90"))  # widening (lower stop) must be refused
        position_service.tighten_stop(position, Decimal("110"))  # tightening (higher stop) is fine
        self.assertEqual(position.stop_loss, Decimal("110"))

    def test_intraday_square_off_closes_the_open_position(self):
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position_service.open_position(self.account, self.contract, order, stop_loss=Decimal("0.01"), target_price=None)

        with patch("apps.market_data.broker_client.BrokerClient.place_order") as mock_place:
            result = tasks.paper_square_off_intraday()
        self.assertTrue(result["squared_off"])
        mock_place.assert_not_called()
        from .models import PaperPosition

        self.assertFalse(PaperPosition.objects.filter(account=self.account, status=PaperPosition.Status.OPEN).exists())
