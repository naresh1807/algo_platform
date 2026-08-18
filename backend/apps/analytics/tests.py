from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.execution.models import OpenPosition
from apps.signals.models import TradingSignal
from common.constants import SignalStatus, SignalType

from .services import compute_daily_performance


class ComputeDailyPerformanceTests(TestCase):
    def test_no_trades_gives_none_win_rate_not_zero(self):
        """
        win_rate=None (not 0.0) with zero trades matters -- 0.0 would
        misleadingly read as "traded and lost every time" on the
        dashboard, rather than "didn't trade at all today."
        """
        metrics = compute_daily_performance(date.today())
        self.assertEqual(metrics.total_trades, 0)
        self.assertIsNone(metrics.win_rate)

    def test_win_rate_and_expectancy_computed_from_closed_trades(self):
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("100"),
            stop_loss=Decimal("95"), total_score=1, technical_score=1,
            sentiment_score=0, risk_score=1, regime="trending",
            status=SignalStatus.EXECUTED, reason="test",
        )
        today = timezone.now()
        # A winning trade: entered 100, stop 95 (risk=5/unit), closed at 110 (+10/unit = +2R)
        OpenPosition.objects.create(
            signal=signal, symbol="NIFTY", side="long", qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            unrealized_pnl=Decimal("100"), closed_at=today,
        )
        metrics = compute_daily_performance(today.date())
        self.assertEqual(metrics.total_trades, 1)
        self.assertEqual(metrics.win_rate, 1.0)
        self.assertAlmostEqual(metrics.avg_r, 2.0)

    def test_rerunning_replaces_not_duplicates(self):
        for_date = date.today()
        compute_daily_performance(for_date)
        compute_daily_performance(for_date)
        from .models import PerformanceMetrics
        self.assertEqual(PerformanceMetrics.objects.filter(date=for_date).count(), 1)

    def test_net_pnl_is_none_with_no_trades(self):
        metrics = compute_daily_performance(date.today())
        self.assertIsNone(metrics.net_pnl)

    def test_net_pnl_nets_wins_and_losses(self):
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("100"),
            stop_loss=Decimal("95"), total_score=1, technical_score=1,
            sentiment_score=0, risk_score=1, regime="trending",
            status=SignalStatus.EXECUTED, reason="test",
        )
        today = timezone.now()
        OpenPosition.objects.create(
            signal=signal, symbol="NIFTY", side="long", qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            unrealized_pnl=Decimal("100"), closed_at=today,
        )
        OpenPosition.objects.create(
            signal=signal, symbol="NIFTY", side="long", qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            unrealized_pnl=Decimal("-40"), closed_at=today,
        )
        metrics = compute_daily_performance(today.date())
        self.assertEqual(metrics.net_pnl, Decimal("60.00"))


class DailyPnLReportViewTests(TestCase):
    """apps.analytics.views.DailyPnLReportView."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        # IsTraderOrAdmin requires Trader/Admin group membership -- a
        # superuser bypasses that check (common/permissions.py's
        # documented escape hatch), simplest setup for a test that isn't
        # exercising RBAC itself.
        self.user = get_user_model().objects.create_superuser(username="trader1", password="pw", email="t1@example.com")

    def _client(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def test_refreshes_and_returns_today_even_with_no_stored_row(self):
        from .models import PerformanceMetrics

        self.assertFalse(PerformanceMetrics.objects.filter(date=date.today()).exists())

        response = self._client().get("/api/analytics/daily-pnl/", {"days": 7})

        self.assertEqual(response.status_code, 200)
        dates = [row["date"] for row in response.data["results"]]
        self.assertIn(date.today().isoformat(), dates)

    def test_summary_totals_net_pnl_across_the_window(self):
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("100"),
            stop_loss=Decimal("95"), total_score=1, technical_score=1,
            sentiment_score=0, risk_score=1, regime="trending",
            status=SignalStatus.EXECUTED, reason="test",
        )
        OpenPosition.objects.create(
            signal=signal, symbol="NIFTY", side="long", qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            unrealized_pnl=Decimal("250"), closed_at=timezone.now(),
        )

        response = self._client().get("/api/analytics/daily-pnl/", {"days": 7})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.data["summary"]["total_net_pnl"]), Decimal("250.00"))
        self.assertEqual(response.data["summary"]["total_trades"], 1)
