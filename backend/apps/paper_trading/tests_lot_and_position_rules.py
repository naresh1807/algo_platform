"""
Scenarios 5-13: one lot, dynamic lot size, one open position, one
pending entry order, unlimited sequential trades after close,
averaging/pyramiding/martingale/option-selling all rejected.
"""

from __future__ import annotations
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import TestCase

from . import test_support
from .models import PaperOrder, PaperPosition, get_or_create_account
from .services import execution_engine, paper_risk_engine, position_service


class LotAndPositionRuleTests(TestCase):
    def setUp(self):
        test_support.seed_charge_rules()
        self.account = get_or_create_account()
        self.contracts, self.expiry = test_support.seed_option_chain()
        self.contract = self.contracts[(list(self.contracts.keys())[0][0], "CE")]
        self.market_open_patch = patch("apps.market_data.market_hours.is_market_open", return_value=(True, ""))
        self.market_open_patch.start()
        self.addCleanup(self.market_open_patch.stop)

    def test_exactly_one_lot_is_used(self):
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size * settings.LOTS_PER_TRADE)
        self.assertEqual(order.filled_quantity, self.contract.lot_size)

    def test_dynamic_lot_size_comes_from_the_contract_master_not_a_constant(self):
        """A contract with an unusual, non-standard lot size must drive
        the order quantity directly -- never a hardcoded NIFTY/BANKNIFTY
        constant."""
        odd_lot_contracts, _ = test_support.seed_option_chain(expiry_offset_days=10)
        contract = list(odd_lot_contracts.values())[0]
        contract.lot_size = 33
        contract.save(update_fields=["lot_size"])
        order = execution_engine.submit_entry_order(self.account, contract, 33 * settings.LOTS_PER_TRADE)
        self.assertEqual(order.quantity, 33)
        self.assertEqual(order.filled_quantity, 33)

    def test_only_one_position_can_be_open_at_a_time(self):
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position_service.open_position(self.account, self.contract, order, stop_loss=Decimal("1"), target_price=None)

        self.assertTrue(paper_risk_engine.has_open_position(self.account))
        with self.assertRaises(PermissionError):
            paper_risk_engine.assert_never_averaging_pyramiding_martingale(self.account, "open a second entry")

    def test_db_level_constraint_rejects_a_second_open_position_row(self):
        """Belt-and-suspenders: even bypassing the SERVICE-layer guard
        (assert_never_averaging_pyramiding_martingale) entirely and
        writing a second `status=open` PaperPosition directly via the
        ORM, the DB constraint itself still refuses it (spec's own
        "row-level locking to prevent duplicate orders" requirement,
        enforced structurally, not just in application code)."""
        order1 = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position_service.open_position(self.account, self.contract, order1, stop_loss=Decimal("1"), target_price=None)

        order2 = PaperOrder.objects.create(
            account=self.account, contract=self.contract, purpose=PaperOrder.Purpose.ENTRY,
            side=PaperOrder.Side.BUY, order_type=PaperOrder.OrderType.MARKET, quantity=self.contract.lot_size,
            lot_size_snapshot=self.contract.lot_size, status=PaperOrder.Status.FILLED,
            filled_quantity=self.contract.lot_size, average_fill_price=Decimal("100"),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaperPosition.objects.create(
                    account=self.account, contract=self.contract, entry_order=order2,
                    quantity=self.contract.lot_size, average_entry_price=Decimal("100"),
                    initial_stop_loss=Decimal("90"), stop_loss=Decimal("90"),
                )

    def test_db_level_constraint_rejects_a_second_pending_entry_order(self):
        """Same DB-level belt-and-suspenders for PaperOrder.pending_entry_marker."""
        PaperOrder.objects.create(
            account=self.account, contract=self.contract, purpose=PaperOrder.Purpose.ENTRY,
            side=PaperOrder.Side.BUY, order_type=PaperOrder.OrderType.MARKET, quantity=self.contract.lot_size,
            lot_size_snapshot=self.contract.lot_size, status=PaperOrder.Status.OPEN, pending_entry_marker=1,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaperOrder.objects.create(
                    account=self.account, contract=self.contract, purpose=PaperOrder.Purpose.ENTRY,
                    side=PaperOrder.Side.BUY, order_type=PaperOrder.OrderType.MARKET, quantity=self.contract.lot_size,
                    lot_size_snapshot=self.contract.lot_size, status=PaperOrder.Status.OPEN, pending_entry_marker=1,
                )

    def test_unlimited_sequential_trades_allowed_after_a_position_closes(self):
        order1 = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position1 = position_service.open_position(self.account, self.contract, order1, stop_loss=Decimal("1"), target_price=None)
        position_service.close_position(self.account, position1, "manual_test_close")

        self.assertFalse(paper_risk_engine.has_open_position(self.account))
        # A fresh entry after close must succeed -- no artificial daily cap
        # blocks it (MAX_TRADES_PER_DAY defaults to 0 = unlimited).
        order2 = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position2 = position_service.open_position(self.account, self.contract, order2, stop_loss=Decimal("1"), target_price=None)
        self.assertNotEqual(position1.pk, position2.pk)
        self.assertEqual(settings.MAX_TRADES_PER_DAY, 0)

    def test_averaging_and_pyramiding_are_structurally_rejected(self):
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position_service.open_position(self.account, self.contract, order, stop_loss=Decimal("1"), target_price=None)
        # PermissionError, not OrderRejected -- assert_never_averaging_
        # pyramiding_martingale is a hard structural guard raised BEFORE
        # any of the normal order-validation/rejection flow runs.
        with self.assertRaises(PermissionError):
            execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)

    def test_averaging_pyramiding_martingale_option_selling_flags_are_hard_pinned_false(self):
        self.assertFalse(settings.ALLOW_AVERAGING)
        self.assertFalse(settings.ALLOW_PYRAMIDING)
        self.assertFalse(settings.ALLOW_MARTINGALE)
        self.assertFalse(settings.ALLOW_OPTION_SELLING)

    def test_martingale_quantity_never_scales_up_after_a_loss(self):
        """Two consecutive losing trades must not change the THIRD
        trade's order quantity -- it is always exactly one lot,
        regardless of loss history."""
        for _ in range(2):
            order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
            position = position_service.open_position(self.account, self.contract, order, stop_loss=order.average_fill_price - Decimal("0.01"), target_price=None)
            # force a loss by pushing the price to a fresh, lower snapshot
            from apps.options.models import OptionChainSnapshot
            from django.utils import timezone
            OptionChainSnapshot.objects.create(
                contract=self.contract, timestamp=timezone.now(),
                ltp=position.stop_loss - Decimal("1"), open_interest=5000, change_in_oi=0, volume=2000,
                iv=Decimal("15.0"), bid=position.stop_loss - Decimal("1.5"), ask=position.stop_loss - Decimal("0.5"),
            )
            position_service.close_position(self.account, position, "stop_loss")

        order3 = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        self.assertEqual(order3.quantity, self.contract.lot_size)  # unchanged -- never doubled

    def test_entry_orders_are_always_buy_never_sell(self):
        """ALLOW_OPTION_SELLING=false is enforced structurally: entry
        orders can only ever be BUY (opening a long), never SELL
        (naked-writing an option)."""
        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        self.assertEqual(order.side, PaperOrder.Side.BUY)
