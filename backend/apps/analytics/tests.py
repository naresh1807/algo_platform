from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
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
        # Two separate signals -- OpenPosition.signal is unique (see
        # execution.migrations.0005_brokerorder_openposition_signal_unique),
        # so two closed positions on the same day can no longer share one
        # signal row the way this test originally assumed.
        def _make_signal():
            return TradingSignal.objects.create(
                symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("100"),
                stop_loss=Decimal("95"), total_score=1, technical_score=1,
                sentiment_score=0, risk_score=1, regime="trending",
                status=SignalStatus.EXECUTED, reason="test",
            )

        today = timezone.now()
        OpenPosition.objects.create(
            signal=_make_signal(), symbol="NIFTY", side="long", qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            unrealized_pnl=Decimal("100"), closed_at=today,
        )
        OpenPosition.objects.create(
            signal=_make_signal(), symbol="NIFTY", side="long", qty=10,
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


class SortinoCalmarTests(TestCase):
    """
    apps.analytics.services.compute_sortino_ratio/compute_calmar_ratio --
    hand-verified against a known 5-point equity curve (same fixture,
    same math, cross-checked with plain Python outside the test).
    """

    def _seed_equity_curve(self):
        from apps.risk.models import EquitySnapshot

        base = timezone.now() - timedelta(days=4)
        for i, equity in enumerate([100000, 101000, 99000, 102000, 101500]):
            EquitySnapshot.objects.create(timestamp=base + timedelta(days=i), equity=Decimal(str(equity)))

    def test_sortino_ratio_hand_computed(self):
        from .services import compute_sortino_ratio

        self._seed_equity_curve()
        sortino = compute_sortino_ratio(lookback_days=10)
        self.assertAlmostEqual(sortino, 7.76, places=1)

    def test_calmar_ratio_hand_computed(self):
        from .services import compute_calmar_ratio

        self._seed_equity_curve()
        calmar = compute_calmar_ratio(lookback_days=10)
        self.assertAlmostEqual(calmar, 145.98, places=0)

    def test_none_with_insufficient_history(self):
        from .services import compute_calmar_ratio, compute_sortino_ratio

        self.assertIsNone(compute_sortino_ratio(lookback_days=10))
        self.assertIsNone(compute_calmar_ratio(lookback_days=10))

    def test_sortino_none_with_no_downside_observations(self):
        from apps.risk.models import EquitySnapshot

        from .services import compute_sortino_ratio

        base = timezone.now() - timedelta(days=4)
        for i, equity in enumerate([100000, 101000, 102000, 103000]):  # monotonically up, no losing days
            EquitySnapshot.objects.create(timestamp=base + timedelta(days=i), equity=Decimal(str(equity)))
        self.assertIsNone(compute_sortino_ratio(lookback_days=10))


class SharpeRatioViewTests(TestCase):
    """GET /api/analytics/sharpe-ratio/ -- now reports sortino_ratio/calmar_ratio alongside the original sharpe_ratio key, additively."""

    def _client(self):
        # IsTraderOrAdmin requires Trader/Admin group membership -- a
        # superuser is the standard escape hatch (see common.permissions'
        # own docstring), same pattern this file's other permission-gated
        # test (DailyPnLReportViewTests) already uses.
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_superuser(username="trader_sharpe", password="pw", email="sharpe@example.com")
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_response_includes_all_three_ratios(self):
        response = self._client().get("/api/analytics/sharpe-ratio/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("sharpe_ratio", response.data)
        self.assertIn("sortino_ratio", response.data)
        self.assertIn("calmar_ratio", response.data)


class AggregateRMultiplesTests(TestCase):
    """apps.analytics.backtest._aggregate_r_multiples -- pure-function, hand-verified pooled trade statistics."""

    def test_hand_computed_stats(self):
        from apps.analytics.backtest import _aggregate_r_multiples

        result = _aggregate_r_multiples([1.0, 1.0, -1.0, 2.0, -1.0])
        self.assertEqual(result["trade_count"], 5)
        self.assertEqual(result["win_rate"], 0.6)
        self.assertAlmostEqual(result["profit_factor"], 2.0, places=4)  # gross_win=4, gross_loss=2
        self.assertAlmostEqual(result["expectancy_r"], 0.4, places=4)  # sum=2 / 5
        self.assertIsNotNone(result["sharpe_r"])
        self.assertIsNotNone(result["max_drawdown_r"])

    def test_empty_list(self):
        from apps.analytics.backtest import _aggregate_r_multiples

        result = _aggregate_r_multiples([])
        self.assertEqual(result["trade_count"], 0)
        self.assertIsNone(result["win_rate"])

    def test_no_downside_gives_none_sortino_and_none_calmar(self):
        from apps.analytics.backtest import _aggregate_r_multiples

        result = _aggregate_r_multiples([1.0, 1.0, 1.0])
        self.assertIsNone(result["sortino_r"])
        self.assertIsNone(result["calmar_r"])  # zero drawdown -- undefined, not fabricated


class BacktestSliceBoundaryTests(SimpleTestCase):
    """A train-slice trade must never resolve on a held-out candle."""

    def test_exit_is_capped_at_the_current_slice_end(self):
        import pandas as pd

        from apps.analytics.backtest import _simulate_exit

        # Candle 2 hits the target, but it belongs to the later holdout slice.
        # The training slice ends at candle 1, whose close is a small loss.
        frame = pd.DataFrame([
            {"low": 99.0, "high": 101.0, "close": 100.0},
            {"low": 98.0, "high": 102.0, "close": 99.0},
            {"low": 99.0, "high": 106.0, "close": 105.0},
        ])

        trade = _simulate_exit(
            frame,
            entry_index=0,
            entry_price=100.0,
            stop_loss=95.0,
            target_1=105.0,
            max_holding_bars=48,
            end_index=1,
        )

        self.assertEqual(trade.exit_index, 1)
        self.assertEqual(trade.exit_reason, "time_stop")
        self.assertLess(trade.r_multiple, 0)

    def test_slice_with_no_post_entry_bar_is_rejected(self):
        import pandas as pd

        from apps.analytics.backtest import _simulate_exit

        frame = pd.DataFrame([{"low": 99.0, "high": 101.0, "close": 100.0}])
        with self.assertRaises(ValueError):
            _simulate_exit(frame, 0, 100.0, 95.0, 105.0, 48, end_index=0)


class BacktestTransactionCostTests(SimpleTestCase):
    """The simulator reports both gross outcomes and conservative net fills."""

    @staticmethod
    def _target_frame():
        import pandas as pd

        return pd.DataFrame([
            {"low": 99.0, "high": 101.0, "close": 100.0},
            {"low": 99.0, "high": 106.0, "close": 105.0},
        ])

    def test_explicit_zero_cost_preserves_original_one_r_target(self):
        from apps.analytics.backtest import _simulate_exit

        trade = _simulate_exit(
            self._target_frame(), 0, 100.0, 95.0, 105.0, 48,
            slippage_bps_per_side=0,
            fees_bps_per_side=0,
        )

        self.assertEqual(trade.gross_r_multiple, 1.0)
        self.assertEqual(trade.r_multiple, 1.0)
        self.assertEqual(trade.entry_fill_price, 100.0)
        self.assertEqual(trade.exit_fill_price, 105.0)
        self.assertEqual(trade.fees_price_units, 0.0)
        self.assertEqual(trade.cost_r, 0.0)

    def test_default_costs_reduce_a_target_outcome(self):
        from apps.analytics.backtest import _simulate_exit

        trade = _simulate_exit(self._target_frame(), 0, 100.0, 95.0, 105.0, 48)

        self.assertEqual(trade.gross_r_multiple, 1.0)
        self.assertLess(trade.r_multiple, trade.gross_r_multiple)
        self.assertGreater(trade.entry_fill_price, trade.entry_price)
        self.assertLess(trade.exit_fill_price, trade.exit_price)
        self.assertGreater(trade.fees_price_units, 0)
        self.assertGreater(trade.cost_r, 0)

    def test_costs_make_a_stop_worse_than_minus_one_r(self):
        import pandas as pd

        from apps.analytics.backtest import _simulate_exit

        frame = pd.DataFrame([
            {"low": 99.0, "high": 101.0, "close": 100.0},
            {"low": 94.0, "high": 101.0, "close": 95.0},
        ])
        trade = _simulate_exit(frame, 0, 100.0, 95.0, 105.0, 48)

        self.assertEqual(trade.gross_r_multiple, -1.0)
        self.assertLess(trade.r_multiple, -1.0)

    def test_invalid_cost_input_is_rejected(self):
        from apps.analytics.backtest import _simulate_exit

        with self.assertRaises(ValueError):
            _simulate_exit(
                self._target_frame(), 0, 100.0, 95.0, 105.0, 48,
                slippage_bps_per_side=-0.1,
            )

    def test_result_dict_discloses_cost_model(self):
        from apps.analytics.backtest import BacktestResult

        result = BacktestResult(
            symbol="NIFTY", timeframe="5m", technical_score_threshold=0.7,
            atr_stop_multiplier=1.5, total_candles=100,
            slippage_bps_per_side=7.0, fees_bps_per_side=3.0,
        ).as_dict()

        self.assertEqual(result["cost_model"]["slippage_bps_per_side"], 7.0)
        self.assertEqual(result["cost_model"]["fees_bps_per_side"], 3.0)
        self.assertEqual(result["cost_model"]["assumed_round_trip_drag_bps"], 20.0)
        self.assertTrue(result["cost_model"]["metrics_are_net_of_costs"])


class BacktestCommandCostReportingTests(SimpleTestCase):
    """The management command forwards and visibly reports cost assumptions."""

    def test_formatted_report_labels_gross_and_net_expectancy(self):
        from io import StringIO
        from unittest.mock import patch

        from apps.analytics.management.commands.run_backtest import Command

        report = {
            "symbol": "NIFTY", "timeframe": "5m", "train_candles": 70,
            "test_candles": 30, "selected_technical_score_threshold": 0.7,
            "selected_atr_stop_multiplier": 1.5,
            "cost_model": {
                "slippage_bps_per_side": 7.0, "fees_bps_per_side": 3.0,
                "assumed_round_trip_drag_bps": 20.0, "metrics_are_net_of_costs": True,
            },
            "train_metrics": {
                "total_trades": 10, "win_rate": 0.5, "gross_expectancy_r": 0.2,
                "expectancy_r": 0.1, "profit_factor": 1.2,
            },
            "test_metrics": {
                "total_trades": 4, "win_rate": 0.5, "gross_expectancy_r": 0.1,
                "expectancy_r": 0.0, "profit_factor": 1.0,
            },
            "note": "net of costs",
        }
        stdout = StringIO()
        with patch(
            "apps.analytics.management.commands.run_backtest.walk_forward_backtest",
            return_value=report,
        ) as mocked_backtest:
            Command(stdout=stdout).handle(
                symbol="NIFTY", timeframe="5m", thresholds="0.7",
                atr_multipliers="1.5", test_fraction=0.3,
                slippage_bps_per_side=7.0, fees_bps_per_side=3.0, json=False,
            )

        mocked_backtest.assert_called_once_with(
            "NIFTY", "5m", technical_score_thresholds=[0.7],
            atr_stop_multipliers=[1.5], test_fraction=0.3,
            slippage_bps_per_side=7.0, fees_bps_per_side=3.0,
        )
        output = stdout.getvalue()
        self.assertIn("slippage=7.0 bps, fees=3.0 bps", output)
        self.assertIn("gross_expectancy_r=0.2, net_expectancy_r=0.1", output)

    def test_invalid_cost_assumption_becomes_a_command_error(self):
        from io import StringIO
        from unittest.mock import patch

        from django.core.management.base import CommandError

        from apps.analytics.management.commands.run_backtest import Command

        with patch(
            "apps.analytics.management.commands.run_backtest.walk_forward_backtest",
            side_effect=ValueError("slippage_bps_per_side must be non-negative"),
        ), self.assertRaisesMessage(CommandError, "slippage_bps_per_side must be non-negative"):
            Command(stdout=StringIO()).handle(
                symbol="NIFTY", timeframe="5m", thresholds="0.7",
                atr_multipliers="1.5", test_fraction=0.3,
                slippage_bps_per_side=-1.0, fees_bps_per_side=3.0, json=False,
            )


class RollingWalkForwardBacktestTests(TestCase):
    """apps.analytics.backtest.rolling_walk_forward_backtest -- real multi-fold Train->Test->Roll-forward->Repeat."""

    def test_n_folds_less_than_2_raises(self):
        from apps.analytics.backtest import rolling_walk_forward_backtest

        with self.assertRaises(ValueError):
            rolling_walk_forward_backtest("NIFTY", n_folds=1)

    def test_insufficient_data_returns_error(self):
        from apps.analytics.backtest import rolling_walk_forward_backtest

        result = rolling_walk_forward_backtest("NIFTY", n_folds=3)
        self.assertIn("error", result)

    def test_real_run_produces_expected_fold_count(self):
        from apps.market_data.models import HistoricalData
        from apps.analytics.backtest import rolling_walk_forward_backtest

        base = timezone.now() - timedelta(days=5)
        price = 24000.0
        for i in range(600):
            price += 2.0 if i % 3 != 0 else -1.0  # mild upward drift with noise -- real, non-degenerate movement
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="5m", timestamp=base + timedelta(minutes=5 * i),
                open=price, high=price + 5, low=price - 5, close=price, volume=10000 + i, source="test",
            )

        result = rolling_walk_forward_backtest("NIFTY", n_folds=2, min_trades_to_rank=1)
        if "error" in result:
            # A real, valid outcome (no combo cleared min_trades on any
            # fold's train window) -- still exercises the full
            # segment-splitting/looping pipeline without crashing.
            self.assertEqual(result["error"], "no_fold_produced_a_valid_result")
            self.assertEqual(len(result["fold_results"]), 2)
        else:
            self.assertEqual(len(result["fold_results"]), 2)
            self.assertIn("aggregate_out_of_sample_metrics", result)
            self.assertIn("trade_count", result["aggregate_out_of_sample_metrics"])


class PerformanceBreakdownTests(TestCase):
    """
    apps.analytics.services' regime/expiry/option-side/strategy/time-of-day/
    no-trade breakdown functions -- Phase G of the Options Intelligence
    Engine. Fixtures build two closed trades (one CE win, one PE loss) plus
    one underlying-only trade, spanning two regimes and two expiries, so
    every grouping dimension has more than one bucket to prove it actually
    groups rather than just echoing totals.
    """

    def _make_contract(self, strike, option_type, expiry, token):
        from apps.options.models import OptionContract

        return OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=Decimal(str(strike)),
            option_type=option_type, symbol_token=token, tradingsymbol=f"NIFTY{token}",
            lot_size=75,
        )

    def _make_signal(self, regime, option_side, option_contract=None, stop_loss=Decimal("95")):
        return TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.BUY, entry_price=Decimal("100"),
            stop_loss=stop_loss, total_score=1, technical_score=1, sentiment_score=0,
            risk_score=1, regime=regime, status=SignalStatus.EXECUTED, reason="test",
            option_side=option_side, option_contract=option_contract,
        )

    def setUp(self):
        expiry_a = date.today() + timedelta(days=3)
        expiry_b = date.today() + timedelta(days=10)

        ce_contract = self._make_contract(24500, "CE", expiry_a, "TOKEN_CE_1")
        pe_contract = self._make_contract(24400, "PE", expiry_b, "TOKEN_PE_1")

        ce_signal = self._make_signal("trending", "CE", ce_contract)
        pe_signal = self._make_signal("sideways", "PE", pe_contract)
        underlying_signal = self._make_signal("trending", None, None)

        now = timezone.now()
        self.ce_position = OpenPosition.objects.create(
            signal=ce_signal, option_contract=ce_contract, symbol="NIFTY", side="long", qty=75,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            unrealized_pnl=Decimal("500"), closed_at=now,
        )
        self.pe_position = OpenPosition.objects.create(
            signal=pe_signal, option_contract=pe_contract, symbol="NIFTY", side="long", qty=75,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            unrealized_pnl=Decimal("-200"), closed_at=now,
        )
        self.underlying_position = OpenPosition.objects.create(
            signal=underlying_signal, symbol="NIFTY", side="long", qty=50,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            unrealized_pnl=Decimal("150"), closed_at=now,
        )
        # Pin opened_at to a known session phase (auto_now_add ignores the
        # value passed to create(), so it's overwritten via update() after
        # the row exists -- same technique needed anywhere a test wants
        # control over an auto_now_add field).
        opening_time = timezone.make_aware(datetime.combine(date.today(), time(9, 20)))
        OpenPosition.objects.filter(pk=self.ce_position.pk).update(opened_at=opening_time)

    def test_regime_breakdown_groups_by_regime(self):
        from .services import compute_regime_breakdown

        result = compute_regime_breakdown(lookback_days=30)
        by_regime = {row["regime"]: row for row in result}
        self.assertEqual(by_regime["trending"]["trade_count"], 2)  # ce_position + underlying_position
        self.assertEqual(by_regime["sideways"]["trade_count"], 1)
        self.assertEqual(by_regime["sideways"]["win_rate"], 0.0)

    def test_expiry_breakdown_excludes_underlying_only_positions(self):
        from .services import compute_expiry_breakdown

        result = compute_expiry_breakdown(lookback_days=30)
        total_trades = sum(row["trade_count"] for row in result)
        self.assertEqual(total_trades, 2)  # underlying_position (no option_contract) excluded
        self.assertEqual(len(result), 2)  # two distinct expiries

    def test_option_side_breakdown_includes_underlying_bucket(self):
        from .services import compute_option_side_breakdown

        result = compute_option_side_breakdown(lookback_days=30)
        by_side = {row["option_side"]: row for row in result}
        self.assertEqual(by_side["CE"]["trade_count"], 1)
        self.assertEqual(by_side["PE"]["trade_count"], 1)
        self.assertEqual(by_side["UNDERLYING"]["trade_count"], 1)

    def test_strategy_breakdown_maps_option_side_to_executable_strategy(self):
        from .services import compute_strategy_breakdown

        result = compute_strategy_breakdown(lookback_days=30)
        strategies = {row["strategy"] for row in result}
        self.assertEqual(strategies, {"LONG_CALL", "LONG_PUT", "UNDERLYING"})

    def test_time_of_day_breakdown_buckets_the_pinned_position(self):
        from .services import compute_time_of_day_breakdown

        result = compute_time_of_day_breakdown(lookback_days=30)
        phases = {row["phase"] for row in result}
        self.assertIn("opening", phases)

    def test_no_trade_rate_counts_no_trade_signals(self):
        from .services import compute_no_trade_rate

        TradingSignal.objects.create(
            symbol="NIFTY", signal_type=SignalType.NO_TRADE, entry_price=Decimal("100"),
            stop_loss=Decimal("95"), total_score=0, technical_score=0, sentiment_score=0,
            risk_score=0, regime="trending", status=SignalStatus.REJECTED, reason="test",
        )
        result = compute_no_trade_rate(lookback_days=30)
        self.assertEqual(result["no_trade_count"], 1)
        self.assertEqual(result["total_signals"], 4)  # 3 from setUp + 1 here
        self.assertAlmostEqual(result["no_trade_rate"], 0.25)

    def test_no_trade_rate_none_with_no_signals(self):
        from .services import compute_no_trade_rate

        # OpenPosition.signal is PROTECT -- the closed positions from
        # setUp must go first, or deleting their signals raises
        # ProtectedError.
        OpenPosition.objects.all().delete()
        TradingSignal.objects.all().delete()
        result = compute_no_trade_rate(lookback_days=30)
        self.assertIsNone(result["no_trade_rate"])


class PerformanceBreakdownViewTests(TestCase):
    """GET /api/analytics/performance-breakdown/ -- combines all six breakdown functions into one response."""

    def _client(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_superuser(username="trader_breakdown", password="pw", email="b@example.com")
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_response_includes_every_breakdown_key(self):
        response = self._client().get("/api/analytics/performance-breakdown/")
        self.assertEqual(response.status_code, 200)
        for key in ("by_regime", "by_expiry", "by_option_side", "by_strategy", "by_time_of_day", "no_trade"):
            self.assertIn(key, response.data)
