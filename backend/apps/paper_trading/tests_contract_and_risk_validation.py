"""
Scenarios 14-18: expired contracts, invalid tokens, insufficient
capital, stale market data, and market-closed orders are all rejected.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from . import test_support
from .models import PaperAccount, get_or_create_account
from .services import execution_engine, market_data_reader, paper_risk_engine


class ContractAndRiskValidationTests(TestCase):
    def setUp(self):
        test_support.seed_charge_rules()
        self.account = get_or_create_account()
        self.contracts, self.expiry = test_support.seed_option_chain()
        self.contract = self.contracts[(list(self.contracts.keys())[0][0], "CE")]
        self.market_open_patch = patch("apps.market_data.market_hours.is_market_open", return_value=(True, ""))
        self.market_open_patch.start()
        self.addCleanup(self.market_open_patch.stop)

    def test_expired_inactive_contract_is_rejected(self):
        self.contract.is_active = False
        self.contract.save(update_fields=["is_active"])
        with self.assertRaises(execution_engine.OrderRejected) as ctx:
            execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        self.assertIn("expired-contract", str(ctx.exception))

    def test_invalid_lot_size_zero_is_rejected(self):
        """A contract with no valid token/lot size (lot_size=0, the
        model's own documented "not yet synced" default) must never be
        tradeable."""
        self.contract.lot_size = 0
        self.contract.save(update_fields=["lot_size"])
        with self.assertRaises(execution_engine.OrderRejected) as ctx:
            execution_engine.submit_entry_order(self.account, self.contract, 75)
        self.assertIn("invalid-lot-size", str(ctx.exception))

    def test_quantity_not_a_lot_multiple_is_rejected(self):
        with self.assertRaises(execution_engine.OrderRejected) as ctx:
            execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size + 1)
        self.assertIn("invalid-lot-size", str(ctx.exception))

    def test_insufficient_capital_is_rejected(self):
        account = PaperAccount.objects.get(pk=1)
        account.available_cash = Decimal("1.00")
        account.save(update_fields=["available_cash"])
        with self.assertRaises(execution_engine.OrderRejected) as ctx:
            execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        self.assertIn("insufficient-capital", str(ctx.exception))

    def test_risk_engine_pre_entry_check_rejects_when_cash_cannot_cover_one_lot(self):
        account = PaperAccount.objects.get(pk=1)
        account.available_cash = Decimal("1.00")
        account.save(update_fields=["available_cash"])
        quote = market_data_reader.latest_contract_quote(self.contract)
        decision = paper_risk_engine.check_pre_entry(
            account, self.contract, ltp=quote.ltp, bid=quote.bid, ask=quote.ask,
            open_interest=quote.open_interest, volume=quote.volume, feed_age_seconds=1,
        )
        self.assertFalse(decision.approved)
        self.assertIn("insufficient", decision.reason_text.lower())

    def test_stale_market_data_blocks_trading(self):
        from apps.options.models import OptionChainSnapshot

        OptionChainSnapshot.objects.filter(contract=self.contract).delete()
        stale_time = timezone.now() - timedelta(seconds=600)
        OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=stale_time, ltp=Decimal("100"),
            open_interest=5000, change_in_oi=0, volume=2000, iv=Decimal("15"),
            bid=Decimal("99"), ask=Decimal("101"),
        )
        with self.assertRaises(execution_engine.OrderRejected) as ctx:
            execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
        self.assertIn("stale-quote", str(ctx.exception))

    def test_market_closed_orders_are_rejected(self):
        # Nested patch of the SAME target temporarily overrides the
        # setUp-level patch for the duration of this `with` block only.
        with patch("apps.market_data.market_hours.is_market_open", return_value=(False, "weekend")):
            with self.assertRaises(execution_engine.OrderRejected) as ctx:
                execution_engine.submit_entry_order(self.account, self.contract, self.contract.lot_size)
            self.assertIn("market-closed", str(ctx.exception))
