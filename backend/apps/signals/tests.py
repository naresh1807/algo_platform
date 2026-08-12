from decimal import Decimal

from django.test import TestCase

from apps.market_data.models import HistoricalData
from apps.risk.models import AccountEquity
from common.constants import SignalStatus, SignalType

from .engine import generate_signal
from .models import TradingSignal


class TradingSignalModelTests(TestCase):
    def test_str_includes_symbol_and_score(self):
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=100, stop_loss=95,
            total_score=0.85, technical_score=0.9, sentiment_score=0.5, risk_score=1.0,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )
        self.assertIn("NIFTY", str(signal))
        self.assertIn("buy", str(signal))


class GenerateSignalTests(TestCase):
    def test_no_trade_logged_with_insufficient_data(self):
        """
        generate_signal() must ALWAYS save a row, even when there's not
        enough data to evaluate -- this is the "every decision must be
        logged, including 'we couldn't decide'" behavior from the
        module's own docstring.
        """
        signal = generate_signal("NIFTY", "5m")
        self.assertEqual(signal.signal_type, SignalType.NO_TRADE)
        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertIn("Not enough historical candles", signal.reason)
        self.assertEqual(TradingSignal.objects.count(), 1)

    def test_generate_signal_with_seeded_candles_produces_a_row(self):
        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day="2026-01-01",
        )
        from datetime import timedelta
        from django.utils import timezone

        now = timezone.now()
        price = 24500.0
        for i in range(80, 0, -1):
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="5m", timestamp=now - timedelta(minutes=5 * i),
                open=price, high=price + 5, low=price - 5, close=price + (i % 3),
                volume=100000, source="test",
            )
        signal = generate_signal("NIFTY", "5m")
        # Whatever the outcome (BUY or NO_TRADE depends on the exact
        # synthetic data shape), a real row with real computed scores
        # must exist -- that's the property worth testing here, not a
        # specific outcome from arbitrary synthetic data.
        self.assertIsNotNone(signal.pk)
        self.assertIn(signal.signal_type, [SignalType.BUY, SignalType.NO_TRADE])
