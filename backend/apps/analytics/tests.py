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
