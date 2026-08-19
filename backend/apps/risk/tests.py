from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from .engine import check_pre_trade
from .models import AccountEquity, KillSwitchState, RiskEvent


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

    def test_get_equity_rolls_daily_baseline_once_without_changing_equity(self):
        from datetime import timedelta

        from apps.execution.models import ExecutionModeSetting

        from .engine import get_equity

        today = timezone.localdate()
        ExecutionModeSetting.objects.create(pk=1, mode=ExecutionModeSetting.Mode.PAPER)
        AccountEquity.objects.create(
            current_equity=Decimal("97500"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("102000"), trading_day=today - timedelta(days=1),
            source_mode=ExecutionModeSetting.Mode.PAPER,
        )

        first = get_equity()
        second = get_equity()

        self.assertEqual(first.current_equity, Decimal("97500"))
        self.assertEqual(first.daily_start_equity, Decimal("97500"))
        self.assertEqual(first.trading_day, today)
        self.assertEqual(second.daily_start_equity, Decimal("97500"))
        self.assertEqual(
            RiskEvent.objects.filter(event_type="daily_equity_rollover").count(), 1,
        )

    def test_switching_from_live_to_paper_resets_risk_provenance(self):
        from apps.execution.models import ExecutionModeSetting

        from .engine import get_equity

        ExecutionModeSetting.objects.create(pk=1, mode=ExecutionModeSetting.Mode.PAPER)
        AccountEquity.objects.create(
            current_equity=Decimal("99000"), daily_start_equity=Decimal("105000"),
            peak_equity=Decimal("110000"), consecutive_losses=2,
            trading_day=timezone.localdate(), source_mode=ExecutionModeSetting.Mode.LIVE,
            last_broker_sync_at=timezone.now(),
        )

        equity = get_equity()

        self.assertEqual(equity.source_mode, ExecutionModeSetting.Mode.PAPER)
        self.assertEqual(equity.daily_start_equity, Decimal("99000"))
        self.assertEqual(equity.peak_equity, Decimal("99000"))
        self.assertEqual(equity.consecutive_losses, 0)
        self.assertIsNone(equity.last_broker_sync_at)


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
        # Option-premium-scale entry/stop, not a raw index price -- this
        # is what every real caller of check_pre_trade actually passes
        # (apps.options.index_direction_strategy and apps.learning.
        # scalp_execution both price entry_price/stop_loss in OPTION
        # PREMIUM terms, never the underlying index level -- see their
        # own option_entry_price/option_stop_loss variables). A raw
        # index price like 24500 as "entry_price" would mean 1 unit
        # alone (24.5% of this 100000 equity) already blows past the
        # 2% single-symbol exposure limit at ANY stop distance -- not a
        # realistic trade this platform ever actually places.
        decision = check_pre_trade("NIFTY", Decimal("150"), Decimal("120"))
        self.assertTrue(decision.approved)
        self.assertGreater(decision.position_size, 0)

    @override_settings(RISK_HARD_LIMITS={
        "MAX_RISK_PER_TRADE_PCT": 1.0, "MAX_OPEN_RISK_PCT": 6.0,
        "MAX_ONE_SYMBOL_EXPOSURE_PCT": 2.0, "MAX_OPEN_POSITIONS": 5,
        "MAX_DAILY_LOSS_PCT": 3.0, "MAX_CONSECUTIVE_LOSSES": 3,
        "DRAWDOWN_PAUSE_PCT": 15.0, "DRAWDOWN_FLATTEN_PCT": 20.0,
    })
    def test_position_size_capped_not_vetoed_by_single_symbol_exposure(self):
        """
        apps.risk.engine._cap_qty_to_single_symbol_exposure -- a
        risk-based qty that would cost more than the exposure budget
        gets SIZED DOWN to what fits, not rejected outright. 1000 risk /
        30 stop-distance = qty 33 (cost 4950, over the 2000 budget);
        2000 budget / 150 entry = 13 affordable units -- the trade is
        still approved, just smaller.
        """
        decision = check_pre_trade("NIFTY", Decimal("150"), Decimal("120"))
        self.assertTrue(decision.approved)
        self.assertEqual(decision.position_size, 13)
        self.assertTrue(any("capped" in r for r in decision.reasons))

    def test_even_one_unit_over_exposure_budget_is_rejected(self):
        # A genuinely tiny equity account where even a single unit at
        # this entry price blows the exposure budget must still be
        # rejected outright (qty can't go below 1) -- the cap only
        # helps when a smaller-but-nonzero size exists. All three
        # equity fields are set together (not just current_equity) so
        # drawdown_pct stays 0% -- otherwise this would trip the
        # drawdown-flatten check instead of the exposure path this test
        # is actually targeting.
        AccountEquity.objects.filter(pk=1).update(
            current_equity=Decimal("5000"), daily_start_equity=Decimal("5000"), peak_equity=Decimal("5000"),
        )
        decision = check_pre_trade("NIFTY", Decimal("150"), Decimal("120"))
        self.assertFalse(decision.approved)
        self.assertIn("cannot buy even one unit", decision.reason_text)


class EquityCurveViewTests(TestCase):
    """GET /api/risk/equity/curve/ -- apps.risk.views.EquityCurveView, the performance dashboard's raw equity time series."""

    def _client(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_user(username="trader_eq", password="pw")
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_empty_history_returns_empty_results(self):
        response = self._client().get("/api/risk/equity/curve/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])

    def test_returns_snapshots_ascending_by_time(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import EquitySnapshot

        base = timezone.now() - timedelta(days=2)
        EquitySnapshot.objects.create(timestamp=base + timedelta(hours=2), equity=Decimal("101000"))
        EquitySnapshot.objects.create(timestamp=base, equity=Decimal("100000"))

        response = self._client().get("/api/risk/equity/curve/", {"lookback_days": 7})

        self.assertEqual(response.status_code, 200)
        equities = [Decimal(row["equity"]) for row in response.data["results"]]
        self.assertEqual(equities, [Decimal("100000"), Decimal("101000")])
