from datetime import date, datetime, timedelta
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase
from .btst import next_session, evaluate_setup, generate_btst_signals, IST
from .models import BTSTRun


class BTSTTests(SimpleTestCase):
    def test_exit_skips_weekend_and_holiday(self):
        self.assertEqual(next_session(date(2026,10,1)),date(2026,10,5))
        self.assertEqual(next_session(date(2026,10,19)),date(2026,10,21))

    def test_calendar_unknown_year_rejected(self):
        with self.assertRaises(ValueError):next_session(date(2026,12,31))

    def test_trend_breakout_setup(self):
        history=[dict(open=100+i*.1,high=101+i*.1,low=99+i*.1,close=100+i*.1,volume=1000) for i in range(60)]
        bars=[dict(open=106,high=111,low=105,close=110.5,volume=10) for _ in range(315)]
        setup,_=evaluate_setup(history,bars)
        self.assertIsNotNone(setup)
        self.assertLess(setup["stop_loss"],setup["entry_reference"])
        self.assertGreater(setup["target_price"],setup["entry_reference"])

    def history(self):
        return [dict(open=100+i*.1, high=101+i*.1, low=99+i*.1,
                     close=100+i*.1, volume=1000) for i in range(60)]

    def test_inside_day_breakout_without_twenty_day_breakout(self):
        history = self.history()
        history[-2].update(high=106.2, low=103)
        history[-1].update(high=106, low=104)
        bars = [dict(open=105.5, high=106.6, low=104.8, close=106.5, volume=10) for _ in range(315)]
        setup, _ = evaluate_setup(history, bars)
        self.assertIn("inside_day_breakout", setup["evidence"]["strategies"])
        self.assertNotIn("trend_breakout", setup["evidence"]["strategies"])

    def test_ema_pullback_recovery(self):
        history = self.history()
        history[40]["high"] = 110
        bars = [dict(open=105, high=107.1, low=104.9, close=107, volume=10) for _ in range(315)]
        setup, _ = evaluate_setup(history, bars)
        self.assertIn("ema_pullback", setup["evidence"]["strategies"])
        self.assertNotIn("trend_breakout", setup["evidence"]["strategies"])

    def test_opening_range_vwap_breakout(self):
        history = self.history()
        history[40]["high"] = 110
        bars = [dict(open=105.8, high=106, low=105.6, close=105.9, volume=10) for _ in range(30)]
        bars += [dict(open=106, high=107.2, low=106, close=107.1, volume=10) for _ in range(285)]
        setup, _ = evaluate_setup(history, bars)
        self.assertIn("opening_range_vwap", setup["evidence"]["strategies"])
        self.assertNotIn("trend_breakout", setup["evidence"]["strategies"])
        self.assertGreater(setup["entry_reference"], setup["evidence"]["vwap"])

    def test_all_strategies_require_volume(self):
        bars = [dict(open=106, high=111, low=105, close=110.5, volume=0) for _ in range(315)]
        self.assertIsNone(evaluate_setup(self.history(), bars)[0])

    def test_nonfinite_and_malformed_prices_rejected(self):
        for close in [float("nan"), 200]:
            bars = [dict(open=106, high=111, low=105, close=close, volume=10) for _ in range(315)]
            self.assertIsNone(evaluate_setup(self.history(), bars)[0])

    def test_no_signal_with_insufficient_history(self):
        setup,_=evaluate_setup([],[])
        self.assertIsNone(setup)

    @patch("apps.investing.btst.timezone.now")
    def test_outside_window_skips_without_database(self, now):
        now.return_value=datetime(2026,10,6,16,0,tzinfo=IST)
        self.assertEqual(generate_btst_signals()["status"],"skipped")

    @patch("apps.investing.btst.timezone.now")
    def test_holiday_skips(self, now):
        now.return_value=datetime(2026,10,20,14,30,tzinfo=IST)
        self.assertEqual(generate_btst_signals()["status"],"skipped")


class BTSTDuplicateTests(TestCase):
    @patch("apps.investing.btst.timezone.now")
    def test_duplicate_task_does_not_fetch_data(self, now):
        now.return_value=datetime(2026,10,6,14,30,tzinfo=IST)
        BTSTRun.objects.create(trading_date=date(2026,10,6),scheduled_for=now.return_value,status="completed")
        self.assertEqual(generate_btst_signals()["status"],"duplicate")
