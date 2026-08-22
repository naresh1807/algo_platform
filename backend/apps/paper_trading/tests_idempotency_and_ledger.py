"""
Scenarios 24-25: duplicate events do not produce duplicate orders;
account balance and ledger reconcile.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from . import test_support
from .models import PaperLedger, PaperOrder, get_or_create_account
from .services import execution_engine, position_service


class IdempotencyAndLedgerTests(TestCase):
    def setUp(self):
        test_support.seed_charge_rules()
        self.account = get_or_create_account()
        self.contracts, self.expiry = test_support.seed_option_chain()
        self.contract = self.contracts[(list(self.contracts.keys())[0][0], "CE")]
        self.market_open_patch = patch("apps.market_data.market_hours.is_market_open", return_value=(True, ""))
        self.market_open_patch.start()
        self.addCleanup(self.market_open_patch.stop)

    def test_duplicate_idempotency_key_is_rejected_at_the_db_level(self):
        import uuid

        key = uuid.uuid4()
        PaperOrder.objects.create(
            idempotency_key=key, account=self.account, contract=self.contract, purpose=PaperOrder.Purpose.EXIT,
            side=PaperOrder.Side.SELL, order_type=PaperOrder.OrderType.MARKET, quantity=self.contract.lot_size,
            lot_size_snapshot=self.contract.lot_size, status=PaperOrder.Status.FILLED,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaperOrder.objects.create(
                    idempotency_key=key, account=self.account, contract=self.contract, purpose=PaperOrder.Purpose.EXIT,
                    side=PaperOrder.Side.SELL, order_type=PaperOrder.OrderType.MARKET, quantity=self.contract.lot_size,
                    lot_size_snapshot=self.contract.lot_size, status=PaperOrder.Status.FILLED,
                )

    def test_duplicate_signal_events_do_not_open_two_positions(self):
        """Simulates two concurrent 'signal' events racing to open a
        position for the same account -- the second must be rejected by
        the averaging/pyramiding guard + the DB constraint, never open a
        second position."""
        order1 = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position_service.open_position(self.account, self.contract, order1, stop_loss=Decimal("1"), target_price=None)

        with self.assertRaises(PermissionError):
            execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)

        from .models import PaperPosition

        self.assertEqual(PaperPosition.objects.filter(account=self.account, status="open").count(), 1)

    def test_account_balance_and_ledger_reconcile_after_a_full_round_trip(self):
        starting_cash = self.account.available_cash

        order = execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        position = position_service.open_position(self.account, self.contract, order, stop_loss=Decimal("0.01"), target_price=None)

        from apps.options.models import OptionChainSnapshot
        exit_price = position.average_entry_price + Decimal("10")
        OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=timezone.now(), ltp=exit_price, open_interest=5000,
            change_in_oi=0, volume=2000, iv=Decimal("15"), bid=exit_price - Decimal("0.5"), ask=exit_price + Decimal("0.5"),
        )
        position_service.close_position(self.account, position, "manual_test")

        self.account.refresh_from_db()
        ledger_rows = list(PaperLedger.objects.filter(account=self.account).order_by("created_at"))

        # Replaying the ledger from the starting balance must land on
        # EXACTLY the account's own current available_cash -- this is
        # what "reconciles" means: the ledger is not just descriptive,
        # it is the account's own arithmetic history.
        replayed = starting_cash
        for row in ledger_rows:
            replayed += row.amount
        self.assertEqual(replayed, self.account.available_cash)
        self.assertEqual(ledger_rows[-1].balance_after, self.account.available_cash)
        self.assertEqual(self.account.used_capital, Decimal("0.00"))
