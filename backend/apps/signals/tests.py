from decimal import Decimal

from django.test import TestCase

from apps.market_data.models import HistoricalData
from apps.risk.models import AccountEquity
from common.constants import PositionSide, SignalStatus, SignalType

from .engine import (
    _evaluate_bearish_conditions,
    _evaluate_buy_conditions,
    _evaluate_exit_conditions_short,
    _indicator_signal_fields,
    _safe_float,
    generate_signal,
    should_exit_position,
)
from .models import TradingSignal


class SafeFloatTests(TestCase):
    """
    _safe_float / _indicator_signal_fields -- MySQL rejects NaN outright
    ("nan can not be used with MySQL") for both FloatField and
    DecimalField, so a NaN indicator reading (a real possibility from a
    gap in ingested candles, not just theoretical) must become None
    before it ever reaches a TradingSignal.objects.create() call, for
    every caller of _indicator_signal_fields -- apps.signals.engine's
    own generate_signal() AND apps.options.index_direction_strategy.
    """

    def test_nan_becomes_none(self):
        self.assertIsNone(_safe_float(float("nan")))

    def test_none_stays_none(self):
        self.assertIsNone(_safe_float(None))

    def test_ordinary_value_passes_through(self):
        self.assertEqual(_safe_float(42.5), 42.5)

    def test_indicator_signal_fields_sanitizes_a_nan_reading(self):
        ind = {
            "close": 100.0, "rsi": float("nan"), "adx": 25.0, "bb_width": 0.02,
            "relative_volume": 1.2, "atr": 2.0, "macd_hist": 0.5,
            "ema9_slope": 0.1, "ema21_slope": 0.05,
        }
        fields = _indicator_signal_fields(ind)
        self.assertIsNone(fields["ind_rsi"])
        self.assertEqual(fields["ind_adx"], 25.0)  # untouched, non-NaN values pass through normally

    def test_indicator_signal_fields_sanitizes_a_nan_ratio(self):
        # atr itself is NaN, so the derived ind_atr_pct ratio is NaN too
        # -- must also come out None, not silently propagate the NaN
        # through the division.
        ind = {
            "close": 100.0, "rsi": 50.0, "adx": 25.0, "bb_width": 0.02,
            "relative_volume": 1.2, "atr": float("nan"), "macd_hist": 0.5,
            "ema9_slope": 0.1, "ema21_slope": 0.05,
        }
        fields = _indicator_signal_fields(ind)
        self.assertIsNone(fields["ind_atr_pct"])


class TradingSignalModelTests(TestCase):
    def test_str_includes_symbol_and_score(self):
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=100, stop_loss=95,
            total_score=0.85, technical_score=0.9, sentiment_score=0.5, risk_score=1.0,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )
        self.assertIn("NIFTY", str(signal))
        self.assertIn("buy", str(signal))

    def test_option_side_and_strike_price_default_to_null(self):
        # Every equity/index-engine signal (apps.signals.engine) never
        # sets these -- only apps.options.index_direction_strategy's
        # APPROVED signals do -- so the ordinary create-without-them
        # path (every existing call site, including this test file's
        # own fixtures) must keep working unchanged.
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=100, stop_loss=95,
            total_score=0.85, technical_score=0.9, sentiment_score=0.5, risk_score=1.0,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
        )
        self.assertIsNone(signal.option_side)
        self.assertIsNone(signal.strike_price)

    def test_option_side_and_strike_price_round_trip(self):
        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.SELL, entry_price=100, stop_loss=105,
            total_score=0.85, technical_score=0.9, sentiment_score=0.5, risk_score=1.0,
            regime="trending", status=SignalStatus.APPROVED, reason="test",
            option_side="PE", strike_price=Decimal("24400.00"),
        )
        signal.refresh_from_db()
        self.assertEqual(signal.option_side, "PE")
        self.assertEqual(signal.strike_price, Decimal("24400.00"))


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


class BearishConditionsTests(TestCase):
    """_evaluate_bearish_conditions -- mirror of _evaluate_buy_conditions."""

    def test_bearish_conditions_all_true_for_clean_downtrend(self):
        ind = {
            "close": 90, "ema9": 95, "ema21": 100,
            "ema9_slope": -1, "ema21_slope": -0.5,
            "sar": 105, "macd": -2, "macd_signal": -1,
            "macd_hist": -0.5, "macd_hist_prev": -0.3,
            "rsi": 40, "relative_volume": 1.5,
        }
        conditions = _evaluate_bearish_conditions(ind)
        self.assertTrue(all(conditions.values()))

    def test_bearish_conditions_false_for_clean_uptrend_except_shared_volume_condition(self):
        ind = {
            "close": 110, "ema9": 105, "ema21": 100,
            "ema9_slope": 1, "ema21_slope": 0.5,
            "sar": 95, "macd": 2, "macd_signal": 1,
            "macd_hist": 0.5, "macd_hist_prev": 0.3,
            "rsi": 60, "relative_volume": 1.5,
        }
        conditions = _evaluate_bearish_conditions(ind)
        # relative_volume_breakout isn't directional (a volume expansion
        # can accompany either direction) -- it's the one condition
        # _evaluate_buy_conditions and _evaluate_bearish_conditions
        # share verbatim, so it's excluded from the "should all be false
        # in a clean uptrend" check below.
        directional = {k: v for k, v in conditions.items() if k != "relative_volume_breakout"}
        self.assertFalse(any(directional.values()))
        self.assertTrue(conditions["relative_volume_breakout"])
        # Sanity: the same ind is a clean bullish setup, confirming
        # these two functions really are mirror images, not coincidentally
        # both-false on this input.
        self.assertTrue(all(_evaluate_buy_conditions(ind).values()))


class ShortExitConditionsTests(TestCase):
    """_evaluate_exit_conditions_short -- bullish-reversal exit signals for a SHORT."""

    def test_short_exit_conditions_fire_on_bullish_reversal(self):
        ind = {
            "close_above_ema9_streak": 2, "sar": 95, "close": 110,
            "macd_hist": 0.5, "macd_hist_prev": 0.3,
            "rsi": 60, "relative_volume": 0.5,
        }
        conditions = _evaluate_exit_conditions_short(ind)
        self.assertTrue(all(conditions.values()))


class ShouldExitPositionSideTests(TestCase):
    """should_exit_position's side param -- defaults to LONG, accepts SHORT."""

    def test_defaults_to_long_and_no_data_returns_false(self):
        should_exit, reasons = should_exit_position("NOPE_SYMBOL", "5m")
        self.assertFalse(should_exit)

    def test_short_side_no_data_returns_false(self):
        should_exit, reasons = should_exit_position("NOPE_SYMBOL", "5m", PositionSide.SHORT)
        self.assertFalse(should_exit)
