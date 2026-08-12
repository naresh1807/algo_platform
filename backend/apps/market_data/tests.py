from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from .indicators import MIN_CANDLES_REQUIRED, compute_indicators
from .models import HistoricalData


def _make_candle(symbol, timeframe, minutes_ago, close):
    return HistoricalData.objects.create(
        symbol=symbol, timeframe=timeframe,
        timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        open=Decimal(str(close)), high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)), close=Decimal(str(close)),
        volume=10000, source="test",
    )


class HistoricalDataModelTests(TestCase):
    def test_unique_together_prevents_duplicate_candle(self):
        """
        The whole point of unique_together=(symbol, timeframe, timestamp)
        is that re-ingesting the same candle twice must not create a
        duplicate row -- this is what apps.market_data.tasks relies on
        for its bulk_create(ignore_conflicts=True) upsert pattern.
        """
        ts = timezone.now()
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=ts,
            open=100, high=101, low=99, close=100, volume=1000, source="test",
        )
        with self.assertRaises(Exception):
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="5m", timestamp=ts,
                open=200, high=201, low=199, close=200, volume=2000, source="test",
            )


class ComputeIndicatorsTests(TestCase):
    def test_returns_none_with_insufficient_candles(self):
        """
        apps.signals.engine relies on this returning None (not raising)
        when there isn't enough history yet, treating it as a normal
        NO_TRADE state rather than an error.
        """
        for i in range(MIN_CANDLES_REQUIRED - 10):
            _make_candle("NIFTY", "5m", i, 24500 + i)
        self.assertIsNone(compute_indicators("NIFTY", "5m"))

    def test_returns_indicator_dict_with_enough_candles(self):
        for i in range(MIN_CANDLES_REQUIRED + 10, 0, -1):
            _make_candle("NIFTY", "5m", i, 24500 + (i % 5))
        result = compute_indicators("NIFTY", "5m")
        self.assertIsNotNone(result)
        for key in ("close", "ema9", "ema21", "rsi", "atr", "adx", "bb_width", "relative_volume"):
            self.assertIn(key, result)
