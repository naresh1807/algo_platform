from decimal import Decimal

from django.test import TestCase, override_settings

from .engine import check_pre_trade
from .models import AccountEquity, KillSwitchState


class AccountEquityTests(TestCase):
    def test_drawdown_pct_computed_correctly(self):
        equity = AccountEquity.objects.create(
            current_equity=Decimal("90000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day="2026-01-01",
        )
        self.assertAlmostEqual(equity.drawdown_pct, 10.0)

    def test_drawdown_pct_zero_when_at_peak(self):
        equity = AccountEquity.objects.create(
            current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day="2026-01-01",
        )
        self.assertEqual(equity.drawdown_pct, 0.0)

    def test_daily_pnl_pct_negative_on_loss(self):
        equity = AccountEquity.objects.create(
            current_equity=Decimal("97000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day="2026-01-01",
        )
        self.assertAlmostEqual(equity.daily_pnl_pct, -3.0)

    def test_singleton_pattern_always_saves_as_pk_1(self):
        e1 = AccountEquity(
            current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day="2026-01-01",
        )
        e1.save()
        self.assertEqual(e1.pk, 1)


class CheckPreTradeTests(TestCase):
    def setUp(self):
        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day="2026-01-01",
        )

    def test_kill_switch_active_blocks_every_trade(self):
        KillSwitchState.objects.create(pk=1, is_active=True)
        decision = check_pre_trade("NIFTY", Decimal("24500"), Decimal("24400"))
        self.assertFalse(decision.approved)
        self.assertIn("Kill switch is active", decision.reason_text)

    def test_zero_stop_distance_rejected(self):
        decision = check_pre_trade("NIFTY", Decimal("24500"), Decimal("24500"))
        self.assertFalse(decision.approved)

    @override_settings(RISK_HARD_LIMITS={
        "MAX_RISK_PER_TRADE_PCT": 1.0, "MAX_OPEN_RISK_PCT": 6.0,
        "MAX_ONE_SYMBOL_EXPOSURE_PCT": 2.0, "MAX_OPEN_POSITIONS": 5,
        "MAX_DAILY_LOSS_PCT": 3.0, "MAX_CONSECUTIVE_LOSSES": 3,
        "DRAWDOWN_PAUSE_PCT": 15.0, "DRAWDOWN_FLATTEN_PCT": 20.0,
    })
    def test_normal_conditions_approve_with_a_position_size(self):
        decision = check_pre_trade("NIFTY", Decimal("24500"), Decimal("24400"))
        self.assertTrue(decision.approved)
        self.assertGreater(decision.position_size, 0)
