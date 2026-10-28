"""Order-entry regression tests: premium identity, risk gates, and retries."""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.options.models import OptionChainSnapshot, OptionContract
from apps.risk.engine import RiskDecision
from apps.risk.models import AccountEquity, KillSwitchState
from common.constants import SignalStatus

from .models import ExecutionModeSetting, OpenPosition, OptionTradeRequest


@override_settings(
    EXECUTION_MODE="paper", BROKER_MODE="paper", LIVE_TRADING_ENABLED=False,
    PAPER_OPTION_SLIPPAGE_BPS_PER_SIDE="0", PAPER_OPTION_FEES_BPS_PER_SIDE="0",
)
class OptionTradingAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("option_admin", is_superuser=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=timezone.localdate() + timedelta(days=7),
            strike=24000, option_type="CE", symbol_token="test-option",
            tradingsymbol="NIFTY_TEST_CE", lot_size=25,
        )
        self.quote = OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=timezone.now(),
            ltp=100, bid=100, ask=101, open_interest=10000, change_in_oi=0, volume=10000,
        )
        AccountEquity.objects.create(
            pk=1, current_equity=1000000, daily_start_equity=1000000,
            peak_equity=1000000, trading_day=timezone.localdate(), source_mode="paper",
        )
        ExecutionModeSetting.objects.create(pk=1, mode="paper")
        for name, result in [
            ("apps.execution.option_trading.is_market_open", (True, "")),
            ("apps.market_data.market_hours.is_market_open", (True, "")),
            ("apps.risk.engine._check_feed_freshness", (True, "")),
        ]:
            patcher = patch(name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ticket = {
            "contract_id": self.contract.pk, "lots": 1, "stop_loss": "90",
            "target_price": "120", "execution_mode": "paper",
        }

    def buy(self, **changes):
        data = {**self.ticket, "request_id": str(uuid.uuid4()), "action": "buy", **changes}
        return self.client.post("/api/execution/option-orders/", data, format="json"), data

    def test_preview_uses_option_ask_and_whole_lot_quantity(self):
        res = self.client.post("/api/execution/option-preview/", self.ticket, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["premium"], "101.00")
        self.assertEqual(res.data["quantity"], 25)
        self.assertEqual(Decimal(res.data["stop_risk"]), Decimal("275"))

    def test_buys_call_and_put_as_long_option_premium_positions(self):
        res, _ = self.buy()
        self.assertEqual(res.status_code, 201, res.data)
        position = OpenPosition.objects.get()
        self.assertEqual(position.option_contract_id, self.contract.pk)
        self.assertEqual(position.side, "long")
        self.assertEqual(position.symbol, self.contract.tradingsymbol)
        self.assertEqual(position.qty, 25)
        self.assertEqual(position.entry_reference_price, Decimal("101"))
        self.assertEqual(position.signal.status, SignalStatus.EXECUTED)
        self.assertEqual(position.target_price, Decimal("120"))
        # Close the call first so the underlying exposure rule permits the put.
        self.client.post("/api/execution/option-orders/", {
            "request_id": str(uuid.uuid4()), "action": "sell",
            "position_id": position.pk, "execution_mode": "paper",
        }, format="json")
        self.contract.option_type = "PE"
        self.contract.tradingsymbol = "NIFTY_TEST_PE"
        self.contract.save()
        res, _ = self.buy()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["position"]["side"], "long")

    def test_repeated_buy_uuid_returns_original_position(self):
        res, payload = self.buy()
        duplicate = self.client.post("/api/execution/option-orders/", payload, format="json")
        self.assertEqual(duplicate.status_code, 200, duplicate.data)
        self.assertEqual(res.data["position"]["id"], duplicate.data["position"]["id"])
        self.assertEqual(OpenPosition.objects.count(), 1)
        self.assertEqual(OptionTradeRequest.objects.count(), 1)

    def test_uuid_cannot_be_reused_for_a_changed_order_or_other_user(self):
        _, payload = self.buy()
        changed = self.client.post("/api/execution/option-orders/", {**payload, "lots": 2}, format="json")
        self.assertEqual(changed.status_code, 409)
        other = get_user_model().objects.create_user("other_admin", is_superuser=True)
        self.client.force_authenticate(other)
        res = self.client.post("/api/execution/option-orders/", payload, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.client.get("/api/execution/option-orders/").data["results"], [])

    def test_sell_closes_once_and_keeps_equity_idempotent(self):
        res, _ = self.buy()
        payload = {"request_id": str(uuid.uuid4()), "action": "sell", "position_id": res.data["position"]["id"], "execution_mode": "paper"}
        self.quote.bid = 110
        self.quote.ltp = 110
        self.quote.ask = 111
        self.quote.save()
        sold = self.client.post("/api/execution/option-orders/", payload, format="json")
        self.assertEqual(sold.status_code, 201, sold.data)
        self.assertFalse(sold.data["position"]["is_open"])
        equity = AccountEquity.objects.get(pk=1).current_equity
        self.assertEqual(equity, Decimal("1000225"))
        self.client.post("/api/execution/option-orders/", payload, format="json")
        self.assertEqual(AccountEquity.objects.get(pk=1).current_equity, equity)

    def test_sell_is_available_when_kill_switch_is_active(self):
        res, _ = self.buy()
        KillSwitchState.objects.update_or_create(pk=1, defaults={"is_active": True})
        sold = self.client.post("/api/execution/option-orders/", {
            "request_id": str(uuid.uuid4()), "action": "sell",
            "position_id": res.data["position"]["id"], "execution_mode": "paper",
        }, format="json")
        self.assertEqual(sold.status_code, 201, sold.data)

    def test_kill_switch_blocks_new_buys(self):
        KillSwitchState.objects.update_or_create(pk=1, defaults={"is_active": True})
        res, _ = self.buy()
        self.assertEqual(res.status_code, 400)
        self.assertFalse(OpenPosition.objects.exists())

    def test_stale_quote_blocks_preview_and_buy(self):
        self.quote.timestamp = timezone.now() - timedelta(minutes=2)
        self.quote.save()
        res, _ = self.buy()
        self.assertEqual(res.status_code, 400)
        self.assertIn("60 seconds", res.data["message"])
        self.assertFalse(OpenPosition.objects.exists())

    def test_expired_inactive_and_unknown_lot_contracts_are_rejected(self):
        for change in [{"expiry": timezone.localdate() - timedelta(days=1)}, {"is_active": False}, {"lot_size": 0}]:
            original = {key: getattr(self.contract, key) for key in change}
            for key, value in change.items():
                setattr(self.contract, key, value)
            self.contract.save()
            res, _ = self.buy()
            self.assertEqual(res.status_code, 400, res.data)
            for key, value in original.items():
                setattr(self.contract, key, value)
            self.contract.save()
        self.assertFalse(OpenPosition.objects.exists())

    def test_bad_stop_target_fractional_lots_and_crossed_quotes_are_rejected(self):
        for change in [{"stop_loss": "102"}, {"target_price": "100"}, {"lots": 1.5}, {"lots": 0}]:
            res, _ = self.buy(**change)
            self.assertEqual(res.status_code, 400, res.data)
        self.quote.bid = 102
        self.quote.save()
        res, _ = self.buy()
        self.assertEqual(res.status_code, 400)
        self.assertFalse(OpenPosition.objects.exists())

    def test_requested_lots_cannot_exceed_risk_allowance(self):
        with patch("apps.execution.option_trading.check_pre_trade", return_value=RiskDecision(True, 1, position_size=24)):
            res, _ = self.buy()
        self.assertEqual(res.status_code, 400)
        self.assertIn("0 lots", res.data["message"])

    def test_sell_cannot_open_a_short_or_accept_a_contract_ticket(self):
        res = self.client.post("/api/execution/option-orders/", {
            **self.ticket, "request_id": str(uuid.uuid4()), "action": "sell",
        }, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(OpenPosition.objects.exists())

    def test_mode_mismatch_and_disarmed_live_are_rejected(self):
        ExecutionModeSetting.objects.filter(pk=1).update(mode="live")
        res, _ = self.buy()
        self.assertEqual(res.status_code, 400)
        res, _ = self.buy(execution_mode="live", confirm_live=True)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(OpenPosition.objects.exists())

    def test_missing_live_confirmation_is_rejected_before_request_creation(self):
        res, _ = self.buy(execution_mode="live")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(OptionTradeRequest.objects.exists())

    def test_unprivileged_and_anonymous_users_cannot_place_orders(self):
        self.client.force_authenticate(get_user_model().objects.create_user("viewer"))
        res, _ = self.buy()
        self.assertEqual(res.status_code, 403)
        self.client.force_authenticate(None)
        res, _ = self.buy()
        self.assertIn(res.status_code, [401, 403])


    def test_quote_monitor_closes_target_and_stop_without_duplicate_settlement(self):
        from .option_tasks import monitor_manual_option_positions
        res, _ = self.buy()
        self.quote.bid = 121
        self.quote.ask = 122
        self.quote.ltp = 121
        self.quote.save()
        with patch("apps.execution.option_tasks.is_market_open", return_value=(True, "")):
            results = monitor_manual_option_positions()
            self.assertEqual(results[0]["reason"], "Option target hit")
            equity = AccountEquity.objects.get(pk=1).current_equity
            self.assertEqual(monitor_manual_option_positions(), [])
            self.assertEqual(AccountEquity.objects.get(pk=1).current_equity, equity)
        self.quote.bid = 100
        self.quote.ask = 101
        self.quote.ltp = 100
        self.quote.save()
        res, _ = self.buy()
        self.assertEqual(res.status_code, 201, res.data)
        self.quote.bid = 88
        self.quote.ltp = 88
        self.quote.ask = 89
        self.quote.save()
        with patch("apps.execution.option_tasks.is_market_open", return_value=(True, "")):
            self.assertEqual(monitor_manual_option_positions()[0]["reason"], "Option stop-loss hit")

    def test_unknown_submission_is_never_reexecuted_by_retry(self):
        with patch("apps.execution.option_views.execute_buy", side_effect=TimeoutError("lost response")) as opener:
            res, payload = self.buy()
            self.assertEqual(res.status_code, 202)
            self.assertEqual(res.data["status"], "unknown")
            duplicate = self.client.post("/api/execution/option-orders/", payload, format="json")
            self.assertEqual(duplicate.data["status"], "unknown")
            self.assertEqual(opener.call_count, 1)

    def test_risk_rejection_preserves_triggered_kill_switch(self):
        def reject_and_halt(*args):
            KillSwitchState.objects.update_or_create(pk=1, defaults={"is_active": True})
            return RiskDecision(False, 0, reasons=["Drawdown halt"])
        with patch("apps.execution.option_trading.check_pre_trade", side_effect=reject_and_halt):
            res, _ = self.buy()
        self.assertEqual(res.status_code, 400)
        self.assertTrue(KillSwitchState.objects.get(pk=1).is_active)

    @override_settings(LIVE_TRADING_ENABLED=True)
    def test_live_buy_delegates_to_existing_executor_and_recovers_confirmed_fill(self):
        ExecutionModeSetting.objects.filter(pk=1).update(mode="live")
        AccountEquity.objects.filter(pk=1).update(source_mode="live", last_broker_sync_at=timezone.now())
        def fill(signal):
            signal.status = SignalStatus.EXECUTED
            signal.save(update_fields=["status"])
            return OpenPosition.objects.create(
                signal=signal, option_contract=self.contract,
                symbol=self.contract.tradingsymbol, side="long", qty=25,
                entry_price=101, stop_loss=90, target_price=120, execution_mode="live",
            )
        with patch("apps.execution.live_executor.reconcile_broker_orders"), patch(
            "apps.execution.live_executor.reconcile_broker_positions", return_value=(True, "")
        ), patch("apps.execution.live_executor.sync_live_account_equity", return_value={"synced": True}), patch(
            "apps.execution.live_executor.open_position_live", side_effect=fill
        ) as opener:
            res, payload = self.buy(execution_mode="live", confirm_live=True)
            self.assertEqual(res.status_code, 201, res.data)
            self.assertEqual(res.data["position"]["execution_mode"], "live")
            self.client.post("/api/execution/option-orders/", payload, format="json")
            self.assertEqual(opener.call_count, 1)
        row = OptionTradeRequest.objects.get(pk=payload["request_id"])
        row.status = "unknown"
        row.position = None
        row.save()
        history = self.client.get("/api/execution/option-orders/").data["results"][0]
        self.assertEqual(history["status"], "executed")
        self.assertIsNotNone(history["position"])
