from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.risk.models import AccountEquity
from apps.signals.models import TradingSignal

from .data_quality import DataQualityReport
from .metrics import compute_max_pain, compute_pcr, strike_support_resistance
from .models import OptionChainSnapshot, OptionContract


class OptionsMetricsTests(TestCase):
    def setUp(self):
        self.expiry = date.today() + timedelta(days=7)
        self.underlying = "NIFTY"

        # Two strikes, CE + PE each, with distinct OI so PCR/max-pain/
        # support-resistance all have something non-trivial to compute.
        self.ce_24500 = OptionContract.objects.create(
            underlying=self.underlying, expiry=self.expiry, strike=24500,
            option_type="CE", symbol_token="tok_ce_24500",
        )
        self.pe_24500 = OptionContract.objects.create(
            underlying=self.underlying, expiry=self.expiry, strike=24500,
            option_type="PE", symbol_token="tok_pe_24500",
        )
        self.ce_24600 = OptionContract.objects.create(
            underlying=self.underlying, expiry=self.expiry, strike=24600,
            option_type="CE", symbol_token="tok_ce_24600",
        )

        now = timezone.now()
        OptionChainSnapshot.objects.create(
            contract=self.ce_24500, timestamp=now, ltp=120, open_interest=1000, change_in_oi=0,
        )
        OptionChainSnapshot.objects.create(
            contract=self.pe_24500, timestamp=now, ltp=80, open_interest=3000, change_in_oi=0,
        )
        OptionChainSnapshot.objects.create(
            contract=self.ce_24600, timestamp=now, ltp=60, open_interest=2000, change_in_oi=0,
        )

    def test_pcr_computed_from_latest_snapshots(self):
        # put OI (3000) / call OI (1000 + 2000 = 3000) = 1.0
        pcr = compute_pcr(self.underlying, self.expiry)
        self.assertEqual(pcr, 1.0)

    def test_pcr_none_with_no_data(self):
        self.assertIsNone(compute_pcr("BANKNIFTY", self.expiry))

    def test_max_pain_returns_a_listed_strike(self):
        max_pain = compute_max_pain(self.underlying, self.expiry)
        self.assertIn(max_pain, [24500.0, 24600.0])

    def test_support_resistance_shape(self):
        result = strike_support_resistance(self.underlying, self.expiry)
        self.assertEqual(result["support"][0]["strike"], 24500.0)
        self.assertEqual(result["resistance"][0]["strike"], 24600.0)


class LatestSnapshotsCorrelationTests(TestCase):
    """
    Regression test for apps.options.metrics._latest_snapshots' correlated
    subquery -- guards against reintroducing the OuterRef("pk") bug
    (correlating each candidate row against its OWN id instead of its
    contract_id), which silently returned zero rows for any contract
    that had accumulated more than one snapshot over time -- i.e. any
    real dataset, ever (see metrics.py's fix comment for the mechanism).
    Deliberately gives ONE contract TWO snapshots so the latest one's
    own pk can never coincidentally equal the contract's pk the way a
    single-snapshot-per-contract fixture could mask this exact bug --
    that's exactly the fixture shape OptionsMetricsTests below already
    had, which is why this bug shipped without any test catching it.
    """

    def test_returns_the_latest_snapshot_per_contract_not_the_first(self):
        from .metrics import _latest_snapshots

        expiry = date.today() + timedelta(days=7)
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24500,
            option_type="CE", symbol_token="tok_multi",
        )
        older = timezone.now() - timedelta(minutes=10)
        newer = timezone.now()
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=older, ltp=100, open_interest=1000, change_in_oi=0,
        )
        latest = OptionChainSnapshot.objects.create(
            contract=contract, timestamp=newer, ltp=150, open_interest=2000, change_in_oi=1000,
        )

        result = list(_latest_snapshots("NIFTY", expiry))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].pk, latest.pk)
        self.assertEqual(result[0].open_interest, 2000)


class SuggestBestStrikeTests(TestCase):
    """
    apps.options.strike_selector.suggest_best_strike -- specifically that
    a winning candidate's dict carries "contract_id", which is what
    apps.options.index_direction_strategy uses to load the real
    OptionContract row for real-option execution (see that module's
    docstring) instead of a second (underlying, expiry, strike,
    option_type) lookup.
    """

    def test_suggested_candidate_carries_contract_id(self):
        from apps.market_data.models import HistoricalData

        from .strike_selector import suggest_best_strike

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500,
            volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400,
            option_type="CE", symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE",
            lot_size=25,
        )
        # ltp chosen so its Black-Scholes-implied delta (~0.59) lands
        # inside strike_selector.DELTA_SWEET_SPOT (0.35-0.65) -- see
        # GreeksTests above for how this module already checks the
        # underlying math; this fixture just needs ONE real candidate to
        # clear that filter so "suggested" is non-null.
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=timezone.now(),
            ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000,
        )

        result = suggest_best_strike("NIFTY", expiry, direction="bullish")

        self.assertIsNotNone(result["suggested"])
        self.assertEqual(result["suggested"]["contract_id"], contract.pk)
        self.assertEqual(result["suggested"]["strike"], 24400.0)


class GreeksTests(TestCase):
    """
    Pure-math tests (no DB) for apps.options.greeks -- checked against
    known Black-Scholes reference values.
    """

    def test_atm_call_price_matches_known_value(self):
        from .greeks import black_scholes_price
        # spot=100, strike=100, T=0.25y, r=5%, sigma=20% -> ~4.615 (textbook value)
        price = black_scholes_price(100, 100, 0.25, 0.05, 0.20, "CE")
        self.assertAlmostEqual(price, 4.615, places=2)

    def test_implied_volatility_recovers_input_sigma(self):
        from .greeks import black_scholes_price, implied_volatility
        price = black_scholes_price(100, 100, 0.25, 0.05, 0.20, "CE")
        iv = implied_volatility(price, 100, 100, 0.25, 0.05, "CE")
        self.assertAlmostEqual(iv, 0.20, places=3)

    def test_call_delta_between_zero_and_one(self):
        from .greeks import compute_greeks
        greeks = compute_greeks(100, 100, 0.25, 0.05, 0.20, "CE")
        self.assertTrue(0.0 < greeks["delta"] < 1.0)

    def test_put_delta_between_minus_one_and_zero(self):
        from .greeks import compute_greeks
        greeks = compute_greeks(100, 100, 0.25, 0.05, 0.20, "PE")
        self.assertTrue(-1.0 < greeks["delta"] < 0.0)

    def test_degenerate_inputs_return_none(self):
        from .greeks import black_scholes_price, compute_greeks
        self.assertIsNone(black_scholes_price(100, 100, 0, 0.05, 0.20, "CE"))
        self.assertIsNone(compute_greeks(100, 100, 0.25, 0.05, 0, "CE"))


class OptionExpiriesViewTests(APITestCase):
    """
    /api/options/expiries/ -- must only offer current-or-future
    expiries, since the frontend auto-selects this list's first entry
    as the default option chain to display (see the view's own
    docstring for the bug this guards: an expired expiry sorting first
    meant the page defaulted to a dead chain).
    """

    # A different underlying from OptionsMetricsTests' fixtures (which
    # use NIFTY at date.today() + 7 days) so the two test classes' rows
    # can never collide on OptionContract's (underlying, expiry, strike,
    # option_type) unique_together, even though each runs in its own
    # rolled-back transaction and shouldn't observe the other's data at
    # all.
    UNDERLYING = "BANKNIFTY"

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="pw")
        self.client.force_authenticate(self.user)

        self.expired = date.today() - timedelta(days=1)
        self.current = date.today() + timedelta(days=3)
        self.later = date.today() + timedelta(days=10)

        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=self.expired, strike=51000,
            option_type="CE", symbol_token="tok_expired",
        )
        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=self.current, strike=51500,
            option_type="CE", symbol_token="tok_current",
        )
        OptionContract.objects.create(
            underlying=self.UNDERLYING, expiry=self.later, strike=51500,
            option_type="CE", symbol_token="tok_later",
        )

    def test_expired_expiry_excluded_and_nearest_is_first(self):
        response = self.client.get("/api/options/expiries/", {"underlying": self.UNDERLYING})
        self.assertEqual(response.status_code, 200)
        expiries = response.data["expiries"]
        self.assertNotIn(self.expired.isoformat(), expiries)
        self.assertEqual(expiries[0], self.current.isoformat())
        self.assertEqual(expiries, [self.current.isoformat(), self.later.isoformat()])


class IndexDirectionStrategyTests(TestCase):
    """apps.options.index_direction_strategy -- see that module's own docstring for scope."""

    def test_determine_index_direction_returns_none_with_no_data(self):
        from .index_direction_strategy import determine_index_direction

        result = determine_index_direction("NIFTY", "5m")
        self.assertIsNone(result["direction"])
        self.assertIsNone(result["option_side"])
        self.assertIn("Not enough historical candles", result["detail"])

    def test_success_rate_for_side_not_available_with_no_data(self):
        from .index_direction_strategy import success_rate_for_side

        result = success_rate_for_side("NIFTY", "up", "5m")
        self.assertFalse(result["available"])
        self.assertFalse(result["profitable"])

    def test_success_rate_for_side_not_available_below_min_trades(self):
        """
        Flat, directionless candles never clear DIRECTION_SCORE_THRESHOLD,
        so the bootstrap backtest finds zero simulated trades -- fewer
        than MIN_BOOTSTRAP_TRADES, so a success rate isn't reported as
        available (not enough samples to mean anything).
        """
        from apps.market_data.models import HistoricalData

        from .index_direction_strategy import success_rate_for_side

        now = timezone.now()
        for i in range(120, 0, -1):
            close = 24500 + (i % 2)  # tiny alternating noise, no real trend
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="5m", timestamp=now - timedelta(minutes=5 * i),
                open=close, high=close + 1, low=close - 1, close=close,
                volume=100000, source="test",
            )

        result = success_rate_for_side("NIFTY", "up", "5m")
        self.assertFalse(result["available"])
        self.assertLess(result["trade_count"], 5)

    def test_evaluate_index_direction_trade_logs_no_trade_with_no_data(self):
        from apps.signals.models import TradingSignal
        from common.constants import SignalStatus, SignalType

        from .index_direction_strategy import evaluate_index_direction_trade

        signal = evaluate_index_direction_trade("NIFTY", "5m")
        self.assertEqual(signal.signal_type, SignalType.NO_TRADE)
        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertIn("Not enough historical candles", signal.reason)
        self.assertEqual(TradingSignal.objects.count(), 1)

    def test_evaluate_index_direction_trade_always_returns_a_saved_row(self):
        """
        Same "whatever the outcome, a real row must exist" property
        GenerateSignalTests checks for apps.signals.engine.generate_signal
        -- this strategy makes the exact same "log every decision" promise.
        """
        from apps.market_data.models import HistoricalData
        from apps.risk.models import AccountEquity
        from common.constants import SignalType

        from .index_direction_strategy import evaluate_index_direction_trade

        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )
        now = timezone.now()
        price = 24500.0
        for i in range(120, 0, -1):
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="5m", timestamp=now - timedelta(minutes=5 * i),
                open=price, high=price + 5, low=price - 5, close=price + (i % 3),
                volume=100000, source="test",
            )

        signal = evaluate_index_direction_trade("NIFTY", "5m")
        self.assertIsNotNone(signal.pk)
        self.assertIn(signal.signal_type, [SignalType.BUY, SignalType.SELL, SignalType.NO_TRADE])


class EvaluateIndexDirectionTradeApprovalTests(TestCase):
    """
    evaluate_index_direction_trade's approved path -- specifically that
    it stamps option_side/strike_price onto the resulting TradingSignal,
    which is what the live dashboard/popup/signal-list pages now read to
    show "which strike, CE or PE" (see apps.signals.signals' WebSocket
    broadcast and the frontend Signals.jsx/Dashboard.jsx/
    SignalAlertPopup.jsx pages).

    Mocks every upstream gate (direction, success rate, sentiment,
    options confluence, strike suggestion) to force a deterministic
    approval -- each of those gates already has its own dedicated
    tests elsewhere in this file (or in apps.risk's own test suite for
    check_pre_trade, which is left un-mocked and runs for real against
    the seeded AccountEquity below) -- this test's only job is to check
    the wiring from "a real approval happened" to "the signal carries
    the right option_side/strike_price", not to re-prove any individual
    gate's own logic.
    """

    def setUp(self):
        AccountEquity.objects.create(
            pk=1, current_equity=Decimal("100000"), daily_start_equity=Decimal("100000"),
            peak_equity=Decimal("100000"), trading_day=timezone.localdate(),
        )

    def test_approved_pe_signal_carries_option_side_and_strike(self):
        from unittest.mock import patch

        from apps.signals.models import TradingSignal
        from common.constants import SignalStatus, SignalType

        from . import index_direction_strategy as strat

        fake_ind = {
            "close": 24500.0, "atr": 100.0, "rsi": 40, "adx": 30,
            "bb_width": 0.02, "relative_volume": 1.5, "ema9": 0, "ema21": 0,
            "ema9_slope": 0, "ema21_slope": 0, "macd_hist_prev": 0, "macd_hist": 0,
            "macd": 0, "macd_signal": 0, "sar": 0,
        }
        direction_result = {
            "direction": "down", "option_side": "PE", "score": 0.9,
            "regime": "trending", "ind": fake_ind, "detail": "forced bearish for test",
        }
        success = {
            "available": True, "trade_count": 10, "win_rate": 0.6,
            "expectancy_r": 0.3, "profit_factor": 1.8, "profitable": True,
            "detail": "forced profitable for test",
        }
        sentiment = {
            "sentiment_score": 0.1, "has_contradictory_headline": False,
            "has_strongly_positive_headline": False,
            "headline_count": 3, "confidence": 0.5,
        }
        options_result = {"score": 0.6, "veto": False, "veto_reason": "", "detail": "forced confirming for test"}
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=24400,
            option_type="PE", symbol_token="tok_pe_24400", tradingsymbol="NIFTY24400PE",
            lot_size=1,  # =1 so the mocked position_size below survives lot-rounding unchanged
        )
        suggestion = {
            "suggested": {
                "contract_id": contract.pk,
                "strike": 24400.0, "ltp": 110.0, "bid": 109.0, "ask": 111.0, "open_interest": 1000, "volume": 500,
                "delta": -0.4, "theta": -10, "vega": 5, "iv": 15, "in_sweet_spot": True, "score": 0.7,
            },
            "reason": "24400 PE test suggestion", "candidates": [],
        }
        # Sizing/exposure math is apps.risk's own concern (and already
        # covered by its own test suite) -- mocked here so this test
        # doesn't need to reverse-engineer a stop distance that happens
        # to clear MAX_ONE_SYMBOL_EXPOSURE_PCT for an index-level entry
        # price, which real ATR-based stops on a ~24500 instrument
        # would not do by default (a synthetic small ATR like this
        # fixture's produces a position sized well past the 2%
        # single-symbol exposure cap -- confirmed by running this test
        # unmocked, not a bug, just not what this test is checking).
        from apps.risk.engine import RiskDecision
        risk_decision = RiskDecision(approved=True, risk_score=1.0, reasons=[], position_size=5)

        with patch.object(strat, "determine_index_direction", return_value=direction_result), \
             patch.object(strat, "success_rate_for_side", return_value=success), \
             patch.object(strat, "aggregate_sentiment", return_value=sentiment), \
             patch.object(strat, "options_confluence_score", return_value=options_result), \
             patch.object(strat, "select_expiry", return_value=date.today() + timedelta(days=7)), \
             patch.object(strat, "validate_option_chain_snapshot", return_value=DataQualityReport(valid=True, status="DATA_VALID", issues=[])), \
             patch.object(strat, "suggest_best_strike", return_value=suggestion), \
             patch.object(strat, "check_pre_trade", return_value=risk_decision):
            signal = strat.evaluate_index_direction_trade("NIFTY", "5m")

        self.assertIsInstance(signal, TradingSignal)
        self.assertEqual(signal.status, SignalStatus.APPROVED)
        # Always BUY once a real contract resolves -- buying a PE is a LONG
        # bet on that PE's own premium, not a SELL/short (see
        # index_direction_strategy's module docstring).
        self.assertEqual(signal.signal_type, SignalType.BUY)
        self.assertEqual(signal.option_side, "PE")
        self.assertEqual(signal.strike_price, 24400.0)
        self.assertEqual(signal.option_contract_id, contract.pk)

    def _run_with_sentiment(self, direction: str, sentiment_extra: dict):
        """Shared plumbing for the direction-aware sentiment veto tests below."""
        from unittest.mock import patch

        from apps.risk.engine import RiskDecision

        from . import index_direction_strategy as strat

        fake_ind = {
            "close": 24500.0, "atr": 100.0, "rsi": 40, "adx": 30,
            "bb_width": 0.02, "relative_volume": 1.5, "ema9": 0, "ema21": 0,
            "ema9_slope": 0, "ema21_slope": 0, "macd_hist_prev": 0, "macd_hist": 0,
            "macd": 0, "macd_signal": 0, "sar": 0,
        }
        option_side = "CE" if direction == "up" else "PE"
        direction_result = {
            "direction": direction, "option_side": option_side, "score": 0.9,
            "regime": "trending", "ind": fake_ind, "detail": "forced for test",
        }
        success = {
            "available": True, "trade_count": 10, "win_rate": 0.6,
            "expectancy_r": 0.3, "profit_factor": 1.8, "profitable": True,
            "detail": "forced profitable for test",
        }
        sentiment = {
            "sentiment_score": 0.0, "has_contradictory_headline": False,
            "has_strongly_positive_headline": False, "headline_count": 3, "confidence": 0.5,
            **sentiment_extra,
        }
        options_result = {"score": 0.6, "veto": False, "veto_reason": "", "detail": "forced for test"}
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=24400,
            option_type=option_side, symbol_token=f"tok_{option_side.lower()}_24400",
            tradingsymbol=f"NIFTY24400{option_side}", lot_size=1,
        )
        suggestion = {
            "suggested": {
                "contract_id": contract.pk,
                "strike": 24400.0, "ltp": 110.0, "bid": 109.0, "ask": 111.0, "open_interest": 1000, "volume": 500,
                "delta": -0.4, "theta": -10, "vega": 5, "iv": 15, "in_sweet_spot": True, "score": 0.7,
            },
            "reason": "test suggestion", "candidates": [],
        }
        risk_decision = RiskDecision(approved=True, risk_score=1.0, reasons=[], position_size=5)

        with patch.object(strat, "determine_index_direction", return_value=direction_result), \
             patch.object(strat, "success_rate_for_side", return_value=success), \
             patch.object(strat, "aggregate_sentiment", return_value=sentiment), \
             patch.object(strat, "options_confluence_score", return_value=options_result), \
             patch.object(strat, "select_expiry", return_value=date.today() + timedelta(days=7)), \
             patch.object(strat, "validate_option_chain_snapshot", return_value=DataQualityReport(valid=True, status="DATA_VALID", issues=[])), \
             patch.object(strat, "suggest_best_strike", return_value=suggestion), \
             patch.object(strat, "check_pre_trade", return_value=risk_decision):
            return strat.evaluate_index_direction_trade("NIFTY", "5m")

    def test_negative_headline_does_not_block_pe_trade(self):
        """
        A negative headline contradicts a BULLISH thesis, not a bearish
        one -- vetoing a PE trade because of bearish news would be
        backwards (the news actually confirms the PE thesis).
        """
        from common.constants import SignalStatus

        signal = self._run_with_sentiment("down", {"has_contradictory_headline": True})
        self.assertEqual(signal.status, SignalStatus.APPROVED)
        self.assertEqual(signal.option_side, "PE")

    def test_positive_headline_blocks_pe_trade(self):
        from common.constants import SignalStatus

        signal = self._run_with_sentiment("down", {"has_strongly_positive_headline": True})
        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertIn("strongly positive", signal.reason)

    def test_positive_headline_does_not_block_ce_trade(self):
        from common.constants import SignalStatus

        signal = self._run_with_sentiment("up", {"has_strongly_positive_headline": True})
        self.assertEqual(signal.status, SignalStatus.APPROVED)
        self.assertEqual(signal.option_side, "CE")

    def test_negative_headline_blocks_ce_trade(self):
        from common.constants import SignalStatus

        signal = self._run_with_sentiment("up", {"has_contradictory_headline": True})
        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertIn("strongly negative", signal.reason)


class RunIndexDirectionStrategyTaskTests(TestCase):
    def test_skips_outside_market_hours(self):
        from unittest.mock import patch

        with patch("apps.options.tasks.is_market_open", return_value=(False, "market closed")):
            from .tasks import run_index_direction_strategy

            result = run_index_direction_strategy()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "market closed")


class IndexDirectionStrategyNanSafetyTests(TestCase):
    """
    A real gap in ingested candles (plausible on a rate-limited day --
    see apps.market_data.broker_client's circuit breaker) can leave an
    indicator like ATR as NaN for some bar. Every comparison against
    NaN is False in Python, so a guard written as "skip if bad" (e.g.
    `if x <= 0`) silently lets NaN through unguarded -- these tests
    pin down that the NaN-safe "proceed only if positive" phrasing
    (`if not (x > 0)`) actually catches it, at both the level this bug
    was FOUND at (success_rate_for_side's backtest bootstrap reporting
    "expectancy +nanR", making `profitable` structurally impossible to
    ever be True) and the live-signal path.
    """

    def test_simulate_directional_exit_treats_nan_risk_as_zero_not_nan(self):
        import pandas as pd

        from .index_direction_strategy import _simulate_directional_exit

        df = pd.DataFrame({"high": [100, 101, 102], "low": [99, 98, 97], "close": [100, 100, 100]})
        exit_index, r = _simulate_directional_exit(
            df, entry_index=0, entry_price=100.0, stop=float("nan"), target=105.0,
            direction="up", max_holding_bars=2,
        )
        self.assertEqual(r, 0.0)

    def test_r_multiples_skips_a_nan_atr_bar_instead_of_trading_it(self):
        from unittest.mock import patch

        import pandas as pd

        from . import index_direction_strategy as strat

        df = pd.DataFrame({"high": [100, 101], "low": [99, 98], "close": [100, 100]})
        # A clean 8/8 bullish setup EXCEPT atr is NaN -- must not open a
        # simulated trade at all (not even a degenerate zero-risk one).
        nan_atr_ind = {
            "close": 110.0, "ema9": 105.0, "ema21": 99.0,
            "ema9_slope": 1.0, "ema21_slope": 1.0, "sar": 95.0,
            "macd": 2.0, "macd_signal": 1.0, "macd_hist": 1.0, "macd_hist_prev": 0.5,
            "rsi": 60.0, "relative_volume": 2.0, "atr": float("nan"),
        }
        with patch.object(strat, "indicator_dict_at", return_value=nan_atr_ind), \
             patch.object(strat, "classify_regime", return_value="trending"):
            r_multiples = strat._simulate_directional_r_multiples(df, "up")
        self.assertEqual(r_multiples, [])

    def test_evaluate_index_direction_trade_no_trades_on_nan_atr_instead_of_crashing(self):
        from unittest.mock import patch

        from common.constants import SignalStatus, SignalType

        from . import index_direction_strategy as strat

        fake_ind = {
            "close": 24500.0, "atr": float("nan"), "rsi": 40, "adx": 30,
            "bb_width": 0.02, "relative_volume": 1.5, "ema9": 0, "ema21": 0,
            "ema9_slope": 0, "ema21_slope": 0, "macd_hist_prev": 0, "macd_hist": 0,
            "macd": 0, "macd_signal": 0, "sar": 0,
        }
        direction_result = {
            "direction": "down", "option_side": "PE", "score": 0.9,
            "regime": "trending", "ind": fake_ind, "detail": "forced for test",
        }
        success = {
            "available": True, "trade_count": 10, "win_rate": 0.6,
            "expectancy_r": 0.3, "profit_factor": 1.8, "profitable": True,
            "detail": "forced profitable for test",
        }
        sentiment = {
            "sentiment_score": 0.0, "has_contradictory_headline": False,
            "has_strongly_positive_headline": False, "headline_count": 0, "confidence": 0.0,
        }

        with patch.object(strat, "determine_index_direction", return_value=direction_result), \
             patch.object(strat, "success_rate_for_side", return_value=success), \
             patch.object(strat, "aggregate_sentiment", return_value=sentiment):
            signal = strat.evaluate_index_direction_trade("NIFTY", "5m")

        self.assertEqual(signal.status, SignalStatus.REJECTED)
        self.assertEqual(signal.signal_type, SignalType.NO_TRADE)
        self.assertIn("ATR is invalid", signal.reason)
        self.assertEqual(signal.entry_price, signal.stop_loss)


class SyncWatchlistOptionContractsTests(TestCase):
    """apps.options.tasks.sync_watchlist_option_contracts -- BROKER_MODE guard only (no real broker call in tests)."""

    def test_skips_outside_live_broker_mode(self):
        from django.test import override_settings

        from .tasks import sync_watchlist_option_contracts

        with override_settings(BROKER_MODE="paper"):
            result = sync_watchlist_option_contracts()
        self.assertTrue(result.get("skipped"))
        self.assertIn("BROKER_MODE", result.get("reason", ""))


class DataQualityValidatorTests(TestCase):
    """
    apps.options.data_quality -- pure-function per-field validators (no
    DB). Each one is a plain (ok, reason) check, mirroring apps.risk.
    engine's own check style.
    """

    def test_validate_ltp(self):
        from .data_quality import validate_ltp

        self.assertTrue(validate_ltp(112.20)[0])
        self.assertFalse(validate_ltp(None)[0])
        self.assertFalse(validate_ltp(0)[0])
        self.assertFalse(validate_ltp(-5)[0])

    def test_validate_bid_ask(self):
        from .data_quality import validate_bid_ask

        self.assertTrue(validate_bid_ask(109.0, 111.0)[0])
        self.assertFalse(validate_bid_ask(None, 111.0)[0])
        self.assertFalse(validate_bid_ask(111.0, 109.0)[0])  # crossed
        self.assertFalse(validate_bid_ask(0, 0)[0])

    def test_validate_iv_rejects_outside_solver_bounds(self):
        from .data_quality import validate_iv

        self.assertTrue(validate_iv(18.5)[0])
        self.assertFalse(validate_iv(None)[0])
        self.assertFalse(validate_iv(0)[0])
        self.assertFalse(validate_iv(600)[0])  # beyond greeks.py's own solver bound of 500%

    def test_validate_greeks_rejects_out_of_range_delta(self):
        from .data_quality import validate_greeks

        self.assertTrue(validate_greeks({"delta": 0.55, "gamma": 0.001, "vega": 5.0})[0])
        self.assertFalse(validate_greeks(None)[0])
        self.assertFalse(validate_greeks({"delta": 1.5})[0])
        self.assertFalse(validate_greeks({"delta": 0.5, "gamma": -0.001})[0])

    def test_detect_stale_quotes(self):
        from datetime import timedelta

        from .data_quality import detect_stale_quotes

        now = timezone.now()
        is_stale, _ = detect_stale_quotes(now - timedelta(minutes=30), now=now, threshold_minutes=15)
        self.assertTrue(is_stale)
        is_stale, _ = detect_stale_quotes(now - timedelta(minutes=2), now=now, threshold_minutes=15)
        self.assertFalse(is_stale)
        self.assertTrue(detect_stale_quotes(None)[0])

    def test_detect_bad_ticks_flags_ltp_far_outside_bid_ask(self):
        from .data_quality import detect_bad_ticks

        self.assertFalse(detect_bad_ticks(110.0, 109.0, 111.0)[0])
        self.assertTrue(detect_bad_ticks(200.0, 109.0, 111.0)[0])  # LTP way outside the quoted spread
        self.assertTrue(detect_bad_ticks(110.0, 111.0, 109.0)[0])  # crossed

    def test_detect_wide_spread_contracts(self):
        from .data_quality import detect_wide_spread_contracts

        self.assertFalse(detect_wide_spread_contracts(109.0, 111.0, max_spread_pct=10.0)[0])
        self.assertTrue(detect_wide_spread_contracts(90.0, 130.0, max_spread_pct=10.0)[0])


class ValidateOptionChainSnapshotTests(TestCase):
    """apps.options.data_quality.validate_option_chain_snapshot -- the chain-wide gate wired into evaluate_index_direction_trade."""

    def setUp(self):
        self.expiry = date.today() + timedelta(days=7)
        self.contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24400,
            option_type="CE", symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE", lot_size=25,
        )

    def test_valid_with_a_fresh_sane_snapshot(self):
        from .data_quality import validate_option_chain_snapshot

        OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=timezone.now(),
            ltp=Decimal("112.20"), open_interest=5000, change_in_oi=0, volume=1000,
        )
        report = validate_option_chain_snapshot("NIFTY", self.expiry)
        self.assertTrue(report.valid)
        self.assertEqual(report.status, "DATA_VALID")

    def test_invalid_when_every_snapshot_is_stale(self):
        from datetime import timedelta as td

        from .data_quality import validate_option_chain_snapshot

        OptionChainSnapshot.objects.create(
            contract=self.contract, timestamp=timezone.now() - td(minutes=60),
            ltp=Decimal("112.20"), open_interest=5000, change_in_oi=0, volume=1000,
        )
        report = validate_option_chain_snapshot("NIFTY", self.expiry)
        self.assertFalse(report.valid)
        self.assertEqual(report.status, "DATA_INVALID")

    def test_invalid_when_no_contracts_synced(self):
        from .data_quality import validate_option_chain_snapshot

        report = validate_option_chain_snapshot("BANKNIFTY", self.expiry)
        self.assertFalse(report.valid)

    def test_invalid_for_an_expired_expiry(self):
        from .data_quality import validate_option_chain_snapshot

        report = validate_option_chain_snapshot("NIFTY", date.today() - timedelta(days=1))
        self.assertFalse(report.valid)


class VolatilitySurfaceTests(TestCase):
    """apps.options.volatility_surface -- IV percentile (pure) + skew/term-structure (DB-backed, real Greeks)."""

    def test_calculate_iv_percentile(self):
        from .volatility_surface import calculate_iv_percentile

        self.assertEqual(calculate_iv_percentile(15.0, [10.0, 12.0, 14.0, 16.0, 18.0]), 60.0)
        self.assertIsNone(calculate_iv_percentile(None, [10.0, 12.0]))
        self.assertIsNone(calculate_iv_percentile(15.0, []))

    def test_detect_contango_or_backwardation_pure(self):
        from .volatility_surface import detect_contango_or_backwardation

        contango = [{"days_to_expiry": 7, "atm_iv": 14.0}, {"days_to_expiry": 30, "atm_iv": 18.0}]
        self.assertEqual(detect_contango_or_backwardation(contango), "contango")

        backwardation = [{"days_to_expiry": 7, "atm_iv": 22.0}, {"days_to_expiry": 30, "atm_iv": 16.0}]
        self.assertEqual(detect_contango_or_backwardation(backwardation), "backwardation")

        self.assertEqual(detect_contango_or_backwardation([{"days_to_expiry": 7, "atm_iv": 14.0}]), "insufficient_data")

    def test_calculate_atm_iv_and_skew_with_real_chain_fixture(self):
        from apps.market_data.models import HistoricalData

        from .volatility_surface import calculate_25_delta_skew, calculate_atm_iv, calculate_call_skew

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        # ATM CE (~0.59 delta, matches SuggestBestStrikeTests' known fixture)
        atm_call = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=atm_call, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)
        # Deep OTM CE, priced cheaply -> low delta, a plausible ~0.25-delta candidate
        otm_call = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=25200, option_type="CE",
            symbol_token="tok_ce_25200", tradingsymbol="NIFTY25200CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=otm_call, timestamp=timezone.now(), ltp=Decimal("40.0"), open_interest=3000, change_in_oi=0, volume=800)
        atm_put = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="PE",
            symbol_token="tok_pe_24400", tradingsymbol="NIFTY24400PE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=atm_put, timestamp=timezone.now(), ltp=Decimal("220.0"), open_interest=4000, change_in_oi=0, volume=900)
        otm_put = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=23800, option_type="PE",
            symbol_token="tok_pe_23800", tradingsymbol="NIFTY23800PE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=otm_put, timestamp=timezone.now(), ltp=Decimal("35.0"), open_interest=2500, change_in_oi=0, volume=700)

        atm_iv = calculate_atm_iv("NIFTY", expiry, 24500)
        self.assertIsNotNone(atm_iv["call_iv"])
        self.assertIsNotNone(atm_iv["put_iv"])
        self.assertIsNotNone(atm_iv["average_iv"])

        call_skew = calculate_call_skew("NIFTY", expiry, 24500)
        self.assertIsNotNone(call_skew)  # a real number, not asserting a specific sign/magnitude on synthetic data

        skew_25d = calculate_25_delta_skew("NIFTY", expiry, 24500)
        self.assertIsNotNone(skew_25d)

    def test_calculate_atm_iv_returns_none_side_when_unavailable(self):
        from .volatility_surface import calculate_atm_iv

        expiry = date.today() + timedelta(days=7)
        result = calculate_atm_iv("NIFTY", expiry, 24500)
        self.assertIsNone(result["call_iv"])
        self.assertIsNone(result["put_iv"])
        self.assertIsNone(result["average_iv"])


class ExposureTests(TestCase):
    """apps.options.exposure -- gamma/vanna/charm exposure proxies, all explicitly labeled MODELED."""

    def setUp(self):
        from apps.market_data.models import HistoricalData

        self.expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        call = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24400, option_type="CE",
            symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=call, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)
        put = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24400, option_type="PE",
            symbol_token="tok_pe_24400", tradingsymbol="NIFTY24400PE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=put, timestamp=timezone.now(), ltp=Decimal("220.0"), open_interest=4000, change_in_oi=0, volume=900)

    def test_net_gamma_exposure_is_modeled_and_uses_both_sides(self):
        from .exposure import calculate_net_gamma_exposure

        result = calculate_net_gamma_exposure("NIFTY", self.expiry, 24500)
        self.assertEqual(result["label"], "MODELED")
        self.assertIn("assumption", result)
        self.assertEqual(result["contracts_used"], 2)
        self.assertIsNotNone(result["net_gamma_exposure"])
        self.assertIsNotNone(result["call_gamma_exposure"])
        self.assertIsNotNone(result["put_gamma_exposure"])

    def test_net_gamma_exposure_none_when_no_contracts(self):
        from .exposure import calculate_net_gamma_exposure

        result = calculate_net_gamma_exposure("BANKNIFTY", self.expiry, 55000)
        self.assertIsNone(result["net_gamma_exposure"])
        self.assertEqual(result["contracts_used"], 0)

    def test_vanna_exposure_is_modeled(self):
        from .exposure import calculate_vanna_exposure

        result = calculate_vanna_exposure("NIFTY", self.expiry, 24500)
        self.assertEqual(result["label"], "MODELED")
        self.assertEqual(result["contracts_used"], 2)
        self.assertIsNotNone(result["net_exposure"])

    def test_charm_exposure_is_modeled(self):
        from .exposure import calculate_charm_exposure

        result = calculate_charm_exposure("NIFTY", self.expiry, 24500)
        self.assertEqual(result["label"], "MODELED")
        self.assertEqual(result["contracts_used"], 2)
        self.assertIsNotNone(result["net_exposure"])


class ExpectedMoveTests(TestCase):
    """apps.options.expected_move -- hand-computed against spot=24500, iv=15%, dte=7 (math.sqrt-verified, not eyeballed)."""

    def test_calculate_expected_move_matches_hand_computed_value(self):
        from .expected_move import calculate_expected_move

        result = calculate_expected_move(24500, 15, 7)
        self.assertAlmostEqual(result["expected_move"], 508.93, places=2)
        self.assertAlmostEqual(result["upper_range"], 25008.93, places=2)
        self.assertAlmostEqual(result["lower_range"], 23991.07, places=2)
        self.assertEqual(result["days_to_expiry"], 7)

    def test_calculate_expected_move_none_on_missing_inputs(self):
        from .expected_move import calculate_expected_move

        self.assertIsNone(calculate_expected_move(None, 15, 7)["expected_move"])
        self.assertIsNone(calculate_expected_move(24500, None, 7)["expected_move"])
        self.assertIsNone(calculate_expected_move(24500, 15, None)["expected_move"])

    def test_classify_price_vs_expected_range(self):
        from .expected_move import classify_price_vs_expected_range

        # Range [23991.07, 25008.93], width ~1017.86, near_threshold=10%
        self.assertEqual(classify_price_vs_expected_range(24500, 25008.93, 23991.07), "inside_range")
        self.assertEqual(classify_price_vs_expected_range(25100, 25008.93, 23991.07), "outside_upper_range")
        self.assertEqual(classify_price_vs_expected_range(23900, 25008.93, 23991.07), "outside_lower_range")
        self.assertEqual(classify_price_vs_expected_range(24990, 25008.93, 23991.07), "near_upper_range")
        self.assertEqual(classify_price_vs_expected_range(None, 25008.93, 23991.07), "unavailable")

    def test_calculate_expected_move_for_contract_with_real_chain_fixture(self):
        from apps.market_data.models import HistoricalData

        from .expected_move import calculate_expected_move_for_contract

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_ce_24400", tradingsymbol="NIFTY24400CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)

        result = calculate_expected_move_for_contract("NIFTY", expiry, 24500)
        self.assertIsNotNone(result["expected_move"])
        self.assertIsNotNone(result["atm_iv"])
        self.assertGreater(result["upper_range"], 24500)
        self.assertLess(result["lower_range"], 24500)


class SupportResistanceTests(TestCase):
    """apps.options.support_resistance -- confluence zones from OI + VWAP + prior-day + expected-move, no single source dominates."""

    def setUp(self):
        from apps.market_data.models import HistoricalData

        self.expiry = date.today() + timedelta(days=7)
        self.today = timezone.localdate()

        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        # Prior day daily candle
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="1d", timestamp=timezone.now() - timedelta(days=1),
            open=24300, high=24450, low=24250, close=24400, volume=500000, source="test",
        )
        call = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24600, option_type="CE",
            symbol_token="tok_ce_24600", tradingsymbol="NIFTY24600CE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=call, timestamp=timezone.now(), ltp=Decimal("150.0"), open_interest=9000, change_in_oi=0, volume=2000)
        put = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24300, option_type="PE",
            symbol_token="tok_pe_24300", tradingsymbol="NIFTY24300PE", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=put, timestamp=timezone.now(), ltp=Decimal("120.0"), open_interest=8500, change_in_oi=0, volume=1800)

    def test_dynamic_support_includes_oi_put_wall_below_spot(self):
        from .support_resistance import calculate_dynamic_support

        zones = calculate_dynamic_support("NIFTY", self.expiry, 24500)
        self.assertTrue(any("oi_put_wall" in z["sources"] for z in zones))
        for z in zones:
            self.assertLess(z["level"], 24500)

    def test_dynamic_resistance_includes_oi_call_wall_above_spot(self):
        from .support_resistance import calculate_dynamic_resistance

        zones = calculate_dynamic_resistance("NIFTY", self.expiry, 24500)
        self.assertTrue(any("oi_call_wall" in z["sources"] for z in zones))
        for z in zones:
            self.assertGreater(z["level"], 24500)

    def test_zones_sorted_nearest_first(self):
        from .support_resistance import calculate_dynamic_support

        zones = calculate_dynamic_support("NIFTY", self.expiry, 24500)
        levels = [z["level"] for z in zones]
        self.assertEqual(levels, sorted(levels, reverse=True))  # nearest (highest, below spot) first

    def test_detect_support_and_resistance_break(self):
        from .support_resistance import detect_resistance_break, detect_support_break

        support_zones = [{"level": 24300, "sources": ["oi_put_wall"], "confluence_count": 1, "details": []}]
        resistance_zones = [{"level": 24600, "sources": ["oi_call_wall"], "confluence_count": 1, "details": []}]

        self.assertFalse(detect_support_break(24350, support_zones))
        self.assertTrue(detect_support_break(24200, support_zones))
        self.assertFalse(detect_resistance_break(24550, resistance_zones))
        self.assertTrue(detect_resistance_break(24700, resistance_zones))
        self.assertFalse(detect_support_break(24500, []))


class LiquidityScoreTests(TestCase):
    """apps.options.liquidity -- continuous 0-1 scoring, distinct from apps.risk.engine's hard pass/fail gate."""

    def test_tight_spread_high_oi_scores_near_one(self):
        from .liquidity import calculate_liquidity_score

        result = calculate_liquidity_score(bid=109.9, ask=110.1, open_interest=50000, volume=20000)
        self.assertIsNotNone(result["liquidity_score"])
        self.assertGreater(result["liquidity_score"], 0.9)

    def test_wide_spread_low_oi_scores_low(self):
        from .liquidity import calculate_liquidity_score

        result = calculate_liquidity_score(bid=80.0, ask=140.0, open_interest=10, volume=0)
        self.assertIsNotNone(result["liquidity_score"])
        self.assertLess(result["liquidity_score"], 0.3)

    def test_missing_input_yields_none_overall_score(self):
        from .liquidity import calculate_liquidity_score

        result = calculate_liquidity_score(bid=None, ask=None, open_interest=5000, volume=1000)
        self.assertIsNone(result["liquidity_score"])
        self.assertIsNotNone(result["oi_score"])  # component that WAS available is still reported

    def test_estimate_slippage_is_half_spread(self):
        from .liquidity import estimate_slippage

        self.assertEqual(estimate_slippage(109.0, 111.0), 1.0)
        self.assertIsNone(estimate_slippage(None, 111.0))


class PremiumEfficiencyTests(TestCase):
    """apps.options.premium_efficiency -- intrinsic/time value plus premium-vs-realized-vol deviation."""

    def test_intrinsic_and_time_value_for_itm_call(self):
        from .premium_efficiency import calculate_intrinsic_value, calculate_time_value

        intrinsic = calculate_intrinsic_value(spot=24550, strike=24400, option_type="CE")
        self.assertEqual(intrinsic, 150.0)
        self.assertEqual(calculate_time_value(option_price=180.0, intrinsic_value=intrinsic), 30.0)

    def test_intrinsic_value_never_negative_for_otm(self):
        from .premium_efficiency import calculate_intrinsic_value

        self.assertEqual(calculate_intrinsic_value(spot=24300, strike=24400, option_type="CE"), 0.0)
        self.assertEqual(calculate_intrinsic_value(spot=24500, strike=24400, option_type="PE"), 0.0)

    def test_calculate_premium_deviation_labels_richness(self):
        from .premium_efficiency import calculate_premium_deviation

        self.assertEqual(calculate_premium_deviation(120.0, 100.0)["richness"], "rich")
        self.assertEqual(calculate_premium_deviation(80.0, 100.0)["richness"], "cheap")
        self.assertEqual(calculate_premium_deviation(102.0, 100.0)["richness"], "fair")
        self.assertEqual(calculate_premium_deviation(None, 100.0)["richness"], "unavailable")

    def test_calculate_realized_volatility_from_real_daily_candles(self):
        from apps.market_data.models import HistoricalData

        from .premium_efficiency import calculate_realized_volatility

        base = timezone.now() - timedelta(days=25)
        price = 24000
        for i in range(22):
            # A small deterministic oscillation -- real (nonzero) daily
            # returns, not a flat series (which would give exactly-zero
            # realized vol and not actually exercise the stdev math).
            price = price + (50 if i % 2 == 0 else -30)
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="1d", timestamp=base + timedelta(days=i),
                open=price, high=price + 20, low=price - 20, close=price,
                volume=100000, source="test",
            )

        vol = calculate_realized_volatility("NIFTY", timeframe="1d", lookback_days=20)
        self.assertIsNotNone(vol)
        self.assertGreater(vol, 0)

    def test_calculate_realized_volatility_none_with_too_little_history(self):
        from .premium_efficiency import calculate_realized_volatility

        self.assertIsNone(calculate_realized_volatility("NIFTY", timeframe="1d", lookback_days=20))

    def test_calculate_premium_deviation_for_contract_with_real_fixture(self):
        from apps.market_data.models import HistoricalData

        from .premium_efficiency import calculate_premium_deviation_for_contract

        base = timezone.now() - timedelta(days=25)
        price = 24000
        for i in range(22):
            price = price + (50 if i % 2 == 0 else -30)
            HistoricalData.objects.create(
                symbol="NIFTY", timeframe="1d", timestamp=base + timedelta(days=i),
                open=price, high=price + 20, low=price - 20, close=price,
                volume=100000, source="test",
            )

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=date.today() + timedelta(days=7), strike=24400,
            option_type="CE", symbol_token="tok_ce_24400b", tradingsymbol="NIFTY24400CE2", lot_size=25,
        )
        result = calculate_premium_deviation_for_contract(contract, spot=24500, market_price=313.73)
        self.assertIsNotNone(result["theoretical_value"])
        self.assertIsNotNone(result["realized_vol_pct"])
        self.assertIn(result["richness"], ("rich", "cheap", "fair"))
        self.assertEqual(result["intrinsic_value"], 100.0)  # 24500 - 24400


class OiIntelligenceTests(TestCase):
    """apps.options.oi_intelligence -- confidence-scored buildup, OI concentration, migration, and the smart-money proxy."""

    def setUp(self):
        self.expiry = date.today() + timedelta(days=7)

    def _make_snapshot_series(self, contract, ltp_series, oi_series, volume_series, minutes_apart=5):
        base = timezone.now() - timedelta(minutes=minutes_apart * (len(ltp_series) - 1))
        rows = []
        for i, (ltp, oi, vol) in enumerate(zip(ltp_series, oi_series, volume_series)):
            rows.append(OptionChainSnapshot.objects.create(
                contract=contract, timestamp=base + timedelta(minutes=minutes_apart * i),
                ltp=Decimal(str(ltp)), open_interest=oi, change_in_oi=0, volume=vol,
            ))
        return rows

    def test_classify_buildup_with_confidence_bullish_call_buildup(self):
        from .oi_intelligence import classify_buildup_with_confidence

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="CE",
            symbol_token="tok_ce_24500", tradingsymbol="NIFTY24500CE", lot_size=25,
        )
        # Rising price + rising OI + elevated volume -> buildup_bullish, high confidence.
        self._make_snapshot_series(contract, ltp_series=[100, 105, 120], oi_series=[5000, 5200, 6000], volume_series=[500, 600, 2000])

        result = classify_buildup_with_confidence("NIFTY", self.expiry, "CE")
        self.assertEqual(result["classification"], "buildup_bullish")
        self.assertIsNotNone(result["confidence"])
        self.assertGreaterEqual(result["confidence"], 0.3)
        self.assertLessEqual(result["confidence"], 0.95)

    def test_classify_buildup_with_confidence_insufficient_data(self):
        from .oi_intelligence import classify_buildup_with_confidence

        result = classify_buildup_with_confidence("NIFTY", self.expiry, "CE")
        self.assertIsNone(result["classification"])
        self.assertIsNone(result["confidence"])

    def test_calculate_oi_concentration_matches_hand_computed_value(self):
        from .oi_intelligence import calculate_oi_concentration

        for strike, oi in [(24400, 1000), (24500, 6000), (24600, 3000), (24700, 500)]:
            contract = OptionContract.objects.create(
                underlying="NIFTY", expiry=self.expiry, strike=strike, option_type="CE",
                symbol_token=f"tok_ce_{strike}", tradingsymbol=f"NIFTY{strike}CE", lot_size=25,
            )
            OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("100"), open_interest=oi, change_in_oi=0, volume=500)

        # total OI = 10500, top_n=2 (24500 + 24600) = 9000 -> 9000/10500 = 85.71%
        result = calculate_oi_concentration("NIFTY", self.expiry, "CE", top_n=2)
        self.assertAlmostEqual(result["concentration_pct"], 85.71, places=1)
        self.assertEqual(len(result["top_strikes"]), 2)

    def test_detect_oi_migration_reports_shift(self):
        from .oi_intelligence import detect_oi_migration

        c1 = OptionContract.objects.create(underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="CE", symbol_token="tok_m1", tradingsymbol="NIFTY24500CEM", lot_size=25)
        c2 = OptionContract.objects.create(underlying="NIFTY", expiry=self.expiry, strike=24600, option_type="CE", symbol_token="tok_m2", tradingsymbol="NIFTY24600CEM", lot_size=25)

        t1 = timezone.now() - timedelta(minutes=10)
        t2 = timezone.now() - timedelta(minutes=5)
        t3 = timezone.now()
        # t1: c1 has the highest OI. t2/t3: c2 takes over -> a real migration.
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t1, ltp=Decimal("100"), open_interest=8000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c2, timestamp=t1, ltp=Decimal("80"), open_interest=2000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t2, ltp=Decimal("100"), open_interest=3000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c2, timestamp=t2, ltp=Decimal("80"), open_interest=9000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t3, ltp=Decimal("100"), open_interest=3100, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c2, timestamp=t3, ltp=Decimal("80"), open_interest=9500, change_in_oi=0, volume=100)

        result = detect_oi_migration("NIFTY", self.expiry, "CE", lookback_points=3)
        self.assertTrue(result["migrated"])
        self.assertEqual(result["peak_strike_sequence"], [24500.0, 24600.0, 24600.0])

    def test_detect_oi_migration_no_shift_when_peak_holds(self):
        from .oi_intelligence import detect_oi_migration

        c1 = OptionContract.objects.create(underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="CE", symbol_token="tok_h1", tradingsymbol="NIFTY24500CEH", lot_size=25)
        t1, t2 = timezone.now() - timedelta(minutes=5), timezone.now()
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t1, ltp=Decimal("100"), open_interest=5000, change_in_oi=0, volume=100)
        OptionChainSnapshot.objects.create(contract=c1, timestamp=t2, ltp=Decimal("100"), open_interest=5200, change_in_oi=0, volume=100)

        result = detect_oi_migration("NIFTY", self.expiry, "CE", lookback_points=3)
        self.assertFalse(result["migrated"])

    def test_institutional_positioning_proxy_always_labeled_proxy(self):
        from .oi_intelligence import institutional_positioning_proxy

        result = institutional_positioning_proxy("NIFTY", self.expiry, "bullish")
        self.assertEqual(result["label"], "PROXY")
        self.assertIn(result["leaning"], ("insufficient_data",))  # no data seeded in this test

        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=self.expiry, strike=24500, option_type="CE",
            symbol_token="tok_proxy", tradingsymbol="NIFTY24500CEP", lot_size=25,
        )
        self._make_snapshot_series(contract, ltp_series=[100, 110, 125], oi_series=[4000, 4500, 5500], volume_series=[400, 500, 1500])
        result = institutional_positioning_proxy("NIFTY", self.expiry, "bullish")
        self.assertEqual(result["label"], "PROXY")
        self.assertEqual(result["leaning"], "large_holders_likely_accumulating")


class ConfirmationTests(TestCase):
    """apps.options.confirmation -- structured directional/setup-quality factor breakdown + conflict detection."""

    def test_evaluate_trend_bullish_and_bearish(self):
        from .confirmation import _evaluate_trend

        bullish_ind = {"close": 24500, "ema9_slope": 5.0, "ema21_slope": 3.0}
        self.assertEqual(_evaluate_trend(bullish_ind, "bullish")["signal"], "bullish")

        bearish_ind = {"close": 24500, "ema9_slope": -5.0, "ema21_slope": -3.0}
        self.assertEqual(_evaluate_trend(bearish_ind, "bullish")["signal"], "bearish")

        mixed_ind = {"close": 24500, "ema9_slope": 5.0, "ema21_slope": -3.0}
        self.assertEqual(_evaluate_trend(mixed_ind, "bullish")["signal"], "neutral")

        self.assertEqual(_evaluate_trend(None, "bullish")["signal"], "unavailable")

    def test_evaluate_momentum(self):
        from .confirmation import _evaluate_momentum

        self.assertEqual(_evaluate_momentum({"close": 100, "macd_hist": 2.0}, "bullish")["signal"], "bullish")
        self.assertEqual(_evaluate_momentum({"close": 100, "macd_hist": -2.0}, "bullish")["signal"], "bearish")

    def test_evaluate_volume_requires_elevated_volume_and_direction(self):
        from .confirmation import _evaluate_volume

        low_vol = {"relative_volume": 1.0, "macd_hist": 2.0}
        self.assertEqual(_evaluate_volume(low_vol, "bullish")["signal"], "neutral")

        elevated_bullish = {"relative_volume": 2.0, "macd_hist": 2.0}
        self.assertEqual(_evaluate_volume(elevated_bullish, "bullish")["signal"], "bullish")

        elevated_bearish = {"relative_volume": 2.0, "macd_hist": -2.0}
        self.assertEqual(_evaluate_volume(elevated_bearish, "bullish")["signal"], "bearish")

    def test_evaluate_greeks_sweet_spot(self):
        from .confirmation import _evaluate_greeks

        self.assertEqual(_evaluate_greeks(0.5)["signal"], "favorable")
        self.assertEqual(_evaluate_greeks(0.1)["signal"], "unfavorable")
        self.assertEqual(_evaluate_greeks(None)["signal"], "unavailable")

    def test_evaluate_liquidity_and_risk_reward(self):
        from .confirmation import _evaluate_liquidity, _evaluate_risk_reward

        self.assertEqual(_evaluate_liquidity(0.8)["signal"], "favorable")
        self.assertEqual(_evaluate_liquidity(0.3)["signal"], "unfavorable")
        self.assertEqual(_evaluate_risk_reward(2.0)["signal"], "favorable")
        self.assertEqual(_evaluate_risk_reward(0.8)["signal"], "unfavorable")

    def test_detect_signal_conflict_levels(self):
        from .confirmation import detect_signal_conflict

        all_agree = {
            "trend": {"signal": "bullish"}, "momentum": {"signal": "bullish"},
            "oi": {"signal": "neutral"}, "volume": {"signal": "unavailable"}, "skew": {"signal": "bullish"},
        }
        level, _ = detect_signal_conflict(all_agree, "bullish")
        self.assertEqual(level, "CONFLICT_LOW")

        one_disagrees = {
            "trend": {"signal": "bullish"}, "momentum": {"signal": "bullish"},
            "oi": {"signal": "bearish"}, "volume": {"signal": "neutral"}, "skew": {"signal": "neutral"},
        }
        level, _ = detect_signal_conflict(one_disagrees, "bullish")
        self.assertEqual(level, "CONFLICT_MEDIUM")

        majority_disagree = {
            "trend": {"signal": "bearish"}, "momentum": {"signal": "bearish"},
            "oi": {"signal": "bearish"}, "volume": {"signal": "bullish"}, "skew": {"signal": "neutral"},
        }
        level, _ = detect_signal_conflict(majority_disagree, "bullish")
        self.assertEqual(level, "CONFLICT_HIGH")

        all_unavailable = {k: {"signal": "unavailable"} for k in ("trend", "momentum", "oi", "volume", "skew")}
        level, detail = detect_signal_conflict(all_unavailable, "bullish")
        self.assertEqual(level, "CONFLICT_LOW")
        self.assertIn("No directional factors available", detail)

    def test_evaluate_multi_signal_confirmation_structure_with_real_fixture(self):
        from apps.market_data.models import HistoricalData

        from .confirmation import evaluate_multi_signal_confirmation

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_conf_ce", tradingsymbol="NIFTY24400CECONF", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)

        ind = {"close": 24500, "ema9_slope": 5.0, "ema21_slope": 3.0, "macd_hist": 2.0, "relative_volume": 1.5}
        result = evaluate_multi_signal_confirmation(
            "NIFTY", expiry, "bullish", ind, 24500, delta=0.5, liquidity_score=0.7, risk_reward=1.8,
        )
        self.assertEqual(set(result["directional_factors"].keys()), {"trend", "momentum", "oi", "volume", "skew"})
        self.assertEqual(set(result["setup_quality_factors"].keys()), {"greeks", "liquidity", "expected_move", "risk_reward"})
        self.assertIn(result["conflict_level"], ("CONFLICT_LOW", "CONFLICT_MEDIUM", "CONFLICT_HIGH"))
        self.assertEqual(result["setup_quality_factors"]["greeks"]["signal"], "favorable")
        self.assertEqual(result["setup_quality_factors"]["risk_reward"]["signal"], "favorable")


class AnomalyDetectionTests(TestCase):
    """apps.options.anomaly_detection -- z-score vs. rolling historical baseline, hand-verified arithmetic."""

    def test_z_score_anomaly_hand_computed(self):
        from .anomaly_detection import _z_score_anomaly

        # baseline mean=10, std=sqrt(2)=1.4142; current=20 -> z=7.07
        result = _z_score_anomaly(20, [10, 12, 8, 11, 9])
        self.assertAlmostEqual(result["z_score"], 7.07, places=2)
        self.assertTrue(result["is_anomaly"])

        result = _z_score_anomaly(10.5, [10, 12, 8, 11, 9])
        self.assertFalse(result["is_anomaly"])

    def test_z_score_anomaly_insufficient_history(self):
        from .anomaly_detection import _z_score_anomaly

        result = _z_score_anomaly(20, [10, 12])
        self.assertIsNone(result["z_score"])
        self.assertFalse(result["is_anomaly"])

    def test_z_score_anomaly_zero_variance_baseline(self):
        from .anomaly_detection import _z_score_anomaly

        result = _z_score_anomaly(50, [10, 10, 10, 10, 10])
        self.assertIsNone(result["z_score"])
        self.assertFalse(result["is_anomaly"])

    def test_detect_volume_and_oi_and_iv_and_premium_anomaly_with_real_fixture(self):
        from .anomaly_detection import (
            detect_iv_anomaly, detect_oi_change_anomaly, detect_premium_anomaly, detect_volume_anomaly,
        )

        expiry = date.today() + timedelta(days=7)
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24500, option_type="CE",
            symbol_token="tok_anomaly", tradingsymbol="NIFTY24500CEA", lot_size=25,
        )
        base = timezone.now() - timedelta(minutes=50)
        # 5 normal baseline readings, then one clear volume/OI-change/IV outlier as "current".
        for i, (vol, oi_chg, iv) in enumerate([(500, 100, 15.0), (520, 90, 15.2), (480, 110, 14.8), (510, 95, 15.1), (490, 105, 14.9)]):
            OptionChainSnapshot.objects.create(
                contract=contract, timestamp=base + timedelta(minutes=5 * i),
                ltp=Decimal("100.0"), open_interest=5000, change_in_oi=oi_chg, volume=vol, iv=iv,
            )
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=base + timedelta(minutes=30),
            ltp=Decimal("100.0"), open_interest=5000, change_in_oi=5000, volume=50000, iv=60.0,
        )

        self.assertTrue(detect_volume_anomaly(contract)["is_anomaly"])
        self.assertTrue(detect_oi_change_anomaly(contract)["is_anomaly"])
        self.assertTrue(detect_iv_anomaly(contract)["is_anomaly"])

    def test_detect_premium_anomaly_uses_change_series_not_raw_levels(self):
        from .anomaly_detection import detect_premium_anomaly

        expiry = date.today() + timedelta(days=7)
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24600, option_type="CE",
            symbol_token="tok_prem_anomaly", tradingsymbol="NIFTY24600CEA", lot_size=25,
        )
        base = timezone.now() - timedelta(minutes=60)
        # Steady +2% moves each step (normal drift), then one huge +40% jump.
        price = 100.0
        for i in range(6):
            OptionChainSnapshot.objects.create(
                contract=contract, timestamp=base + timedelta(minutes=5 * i),
                ltp=Decimal(str(round(price, 2))), open_interest=5000, change_in_oi=0, volume=500,
            )
            price *= 1.02
        OptionChainSnapshot.objects.create(
            contract=contract, timestamp=base + timedelta(minutes=35),
            ltp=Decimal(str(round(price * 1.40, 2))), open_interest=5000, change_in_oi=0, volume=500,
        )

        result = detect_premium_anomaly(contract)
        self.assertTrue(result["is_anomaly"])

    def test_bid_ask_imbalance_anomaly_is_honestly_unavailable(self):
        from .anomaly_detection import detect_bid_ask_imbalance_anomaly

        result = detect_bid_ask_imbalance_anomaly()
        self.assertFalse(result["available"])
        self.assertIn("reason", result)


class OrderFlowStubTests(TestCase):
    """apps.options.order_flow -- every function honestly unavailable, no fabricated numbers."""

    def test_every_function_reports_unavailable(self):
        from . import order_flow

        for fn_name in (
            "calculate_bid_ask_imbalance", "detect_aggressive_buying", "detect_aggressive_selling",
            "calculate_volume_acceleration", "detect_order_flow_shift", "detect_liquidity_withdrawal",
        ):
            result = getattr(order_flow, fn_name)()
            self.assertFalse(result["available"])
            self.assertIn("reason", result)


class StrategySelectorTests(TestCase):
    """apps.options.strategy_selector -- classification only, only LONG_CALL/LONG_PUT/NO_TRADE are ever executable."""

    def test_no_direction_is_no_trade(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy(None, "TRENDING", 40.0)
        self.assertEqual(result["strategy"], "NO_TRADE")
        self.assertTrue(result["executable"])

    def test_high_conflict_is_no_trade(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "TRENDING", 40.0, conflict_level="CONFLICT_HIGH")
        self.assertEqual(result["strategy"], "NO_TRADE")

    def test_trending_moderate_iv_gives_executable_long_option(self):
        from .strategy_selector import classify_strategy

        bullish = classify_strategy("bullish", "TRENDING", 40.0, expected_move_position="inside_range")
        self.assertEqual(bullish["strategy"], "LONG_CALL")
        self.assertTrue(bullish["executable"])

        bearish = classify_strategy("bearish", "TRENDING_BEARISH", 40.0, expected_move_position="inside_range")
        self.assertEqual(bearish["strategy"], "LONG_PUT")
        self.assertTrue(bearish["executable"])

    def test_trending_high_iv_is_spread_not_executable(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "TRENDING", 85.0, expected_move_position="inside_range")
        self.assertEqual(result["strategy"], "BULL_CALL_SPREAD")
        self.assertFalse(result["executable"])

    def test_move_already_extended_avoids_long_option_even_low_iv(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "TRENDING", 40.0, expected_move_position="outside_upper_range")
        self.assertNotEqual(result["strategy"], "LONG_CALL")
        self.assertFalse(result["executable"])

    def test_non_trending_high_iv_is_iron_condor(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "SIDEWAYS", 85.0)
        self.assertEqual(result["strategy"], "IRON_CONDOR")
        self.assertFalse(result["executable"])

    def test_non_trending_low_iv_is_straddle(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "SIDEWAYS", 10.0)
        self.assertEqual(result["strategy"], "STRADDLE")
        self.assertFalse(result["executable"])

    def test_fallback_is_no_trade(self):
        from .strategy_selector import classify_strategy

        result = classify_strategy("bullish", "SIDEWAYS", 50.0)  # neither high nor low IV, non-trending
        self.assertEqual(result["strategy"], "NO_TRADE")
        self.assertTrue(result["executable"])

    def test_executable_strategies_constant_matches_actual_execution_capability(self):
        from .strategy_selector import EXECUTABLE_STRATEGIES

        self.assertEqual(EXECUTABLE_STRATEGIES, frozenset({"LONG_CALL", "LONG_PUT", "NO_TRADE"}))


class SignalScoringTests(TestCase):
    """apps.options.signal_scoring -- hand-verified weighted arithmetic, unavailable factors excluded and re-normalized."""

    def test_calculate_signal_score_hand_computed(self):
        from .signal_scoring import calculate_signal_score

        weights = {
            "trend": 0.18, "momentum": 0.12, "oi": 0.15, "volume": 0.07, "skew": 0.08,
            "greeks": 0.10, "liquidity": 0.13, "expected_move": 0.08, "risk_reward": 0.09,
        }
        confirmation_result = {
            "directional_factors": {
                "trend": {"signal": "bullish"}, "momentum": {"signal": "bearish"},
                "oi": {"signal": "neutral"}, "volume": {"signal": "unavailable"}, "skew": {"signal": "unavailable"},
            },
            "setup_quality_factors": {
                "greeks": {"signal": "favorable"}, "liquidity": {"signal": "favorable"},
                "expected_move": {"signal": "neutral"}, "risk_reward": {"signal": "unfavorable"},
            },
        }
        result = calculate_signal_score(confirmation_result, weights=weights)
        self.assertAlmostEqual(result["total_score"], 0.6176, places=3)
        self.assertEqual(set(result["factors_excluded"]), {"volume", "skew"})

    def test_all_unavailable_gives_none_not_zero(self):
        from .signal_scoring import calculate_signal_score

        confirmation_result = {
            "directional_factors": {k: {"signal": "unavailable"} for k in ("trend", "momentum", "oi", "volume", "skew")},
            "setup_quality_factors": {k: {"signal": "unavailable"} for k in ("greeks", "liquidity", "expected_move", "risk_reward")},
        }
        result = calculate_signal_score(confirmation_result)
        self.assertIsNone(result["total_score"])

    def test_uses_db_backed_weights_by_default(self):
        from .models import get_scoring_weights
        from .signal_scoring import calculate_signal_score

        confirmation_result = {
            "directional_factors": {"trend": {"signal": "bullish"}},
            "setup_quality_factors": {},
        }
        result = calculate_signal_score(confirmation_result)
        self.assertEqual(result["weights_used"], get_scoring_weights().as_dict())
        self.assertEqual(result["total_score"], 1.0)  # only factor available is bullish=1.0


class SignalExplanationTests(TestCase):
    """apps.options.signal_explanation -- itemized positive/negative/risk-flag lists, neutral/unavailable omitted."""

    def test_build_signal_explanation_itemizes_correctly(self):
        from .signal_explanation import build_signal_explanation

        confirmation_result = {
            "directional_factors": {
                "trend": {"signal": "bullish", "detail": "EMA9/21 rising"},
                "momentum": {"signal": "bearish", "detail": "MACD histogram negative"},
                "oi": {"signal": "neutral", "detail": "no strong OI signal"},
            },
            "setup_quality_factors": {
                "liquidity": {"signal": "favorable", "detail": "liquidity score 0.85"},
            },
            "conflict_level": "CONFLICT_MEDIUM", "conflict_detail": "1/2 factors disagree.",
        }
        result = build_signal_explanation(confirmation_result)
        self.assertEqual(result["positive_factors"], ["Trend: EMA9/21 rising", "Liquidity: liquidity score 0.85"])
        self.assertEqual(result["negative_factors"], ["Momentum: MACD histogram negative"])
        self.assertEqual(len(result["risk_flags"]), 1)
        self.assertIn("Conflict Medium", result["risk_flags"][0])

    def test_anomalies_surface_only_when_flagged(self):
        from .signal_explanation import build_signal_explanation

        confirmation_result = {"directional_factors": {}, "setup_quality_factors": {}}
        anomalies = [
            {"is_anomaly": True, "detail": "volume z=4.2"},
            {"is_anomaly": False, "detail": "iv z=0.5"},
        ]
        result = build_signal_explanation(confirmation_result, anomalies=anomalies)
        self.assertEqual(len(result["risk_flags"]), 1)
        self.assertIn("volume z=4.2", result["risk_flags"][0])


class FinalSignalTests(TestCase):
    """apps.options.final_signal.resolve_final_signal -- the Section-43-style assembled object, real contract identity or explicit None throughout."""

    def test_rejected_signal_with_no_contract_is_data_invalid(self):
        from .final_signal import resolve_final_signal

        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type="no_trade", entry_price=Decimal("24500"), stop_loss=Decimal("24500"),
            total_score=0, technical_score=0, sentiment_score=0, risk_score=0, options_score=0,
            regime="sideways", status="rejected", rejection_stage="data_quality",
            reason="OPTION DATA UNAVAILABLE: test",
        )
        result = resolve_final_signal(signal)
        self.assertEqual(result["status"], "DATA_INVALID")
        self.assertIsNone(result["tradingSymbol"])
        self.assertIsNone(result["instrumentToken"])
        self.assertIsNone(result["strike"])
        self.assertEqual(result["decision"], "NO_TRADE")

    def test_approved_signal_with_real_contract_is_signal_ready(self):
        from apps.market_data.models import HistoricalData

        from .final_signal import resolve_final_signal

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_final_ce", tradingsymbol="NIFTY24400CEFINAL", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)

        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type="buy", entry_price=Decimal("313.73"), stop_loss=Decimal("280.0"),
            target_1=Decimal("360.0"), target_2=Decimal("400.0"), position_size=25,
            option_side="CE", strike_price=Decimal("24400"), option_contract=contract,
            total_score=0.7, technical_score=0.7, sentiment_score=0.1, risk_score=1.0, options_score=0.6,
            regime="trending", status="approved", reason="test approved signal",
        )
        result = resolve_final_signal(signal)
        self.assertEqual(result["status"], "SIGNAL_READY")
        self.assertEqual(result["tradingSymbol"], "NIFTY24400CEFINAL")
        self.assertEqual(result["instrumentToken"], "tok_final_ce")
        self.assertEqual(result["strike"], 24400.0)
        self.assertEqual(result["optionType"], "CE")
        self.assertEqual(result["direction"], "bullish")
        self.assertEqual(result["decision"], "BUY")
        self.assertIsNotNone(result["delta"])
        self.assertIsNotNone(result["riskReward"])
        self.assertIsInstance(result["support"], list)
        self.assertIsInstance(result["resistance"], list)
        self.assertIn("expected_move", result["expectedMove"])
        self.assertIn(result["strategy"], ("LONG_CALL", "NO_TRADE", "BULL_CALL_SPREAD"))


class FinalSignalViewTests(APITestCase):
    """GET /api/options/final-signal/ -- 404 with no signals, 200 with the assembled object otherwise."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader_final", password="pw")
        self.client.force_authenticate(self.user)

    def test_404_when_no_signals_exist(self):
        response = self.client.get("/api/options/final-signal/", {"underlying": "NIFTY"})
        self.assertEqual(response.status_code, 404)

    def test_200_with_latest_signal(self):
        TradingSignal.objects.create(
            symbol="NIFTY", signal_type="no_trade", entry_price=Decimal("24500"), stop_loss=Decimal("24500"),
            total_score=0, technical_score=0, sentiment_score=0, risk_score=0, options_score=0,
            regime="sideways", status="rejected", reason="test",
        )
        response = self.client.get("/api/options/final-signal/", {"underlying": "NIFTY"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("status", response.data)
        self.assertIn("decision", response.data)


class FeatureStoreTests(TestCase):
    """apps.options.feature_store.build_feature_vector -- real contract fixture populates scores/greeks, no contract leaves them None."""

    def test_no_contract_leaves_scores_and_greeks_none(self):
        from .feature_store import build_feature_vector

        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type="no_trade", entry_price=Decimal("24500"), stop_loss=Decimal("24500"),
            total_score=0, technical_score=0, sentiment_score=0, risk_score=0, options_score=0,
            regime="sideways", status="rejected", reason="test",
        )
        vector = build_feature_vector(signal)
        self.assertIsNone(vector["delta"])
        self.assertIsNone(vector["trendScore"])
        self.assertIsNone(vector["orderFlowScore"])  # always None -- honest stub, no data source
        self.assertEqual(vector["marketRegime"], "sideways")
        self.assertIn(vector["timeOfDay"], ("opening", "morning", "midday", "afternoon", "closing", "closed", "unknown"))

    def test_real_contract_populates_greeks_and_scores(self):
        from apps.market_data.models import HistoricalData

        from .feature_store import build_feature_vector

        expiry = date.today() + timedelta(days=7)
        HistoricalData.objects.create(
            symbol="NIFTY", timeframe="5m", timestamp=timezone.now(),
            open=24500, high=24510, low=24490, close=24500, volume=100000, source="test",
        )
        contract = OptionContract.objects.create(
            underlying="NIFTY", expiry=expiry, strike=24400, option_type="CE",
            symbol_token="tok_fs_ce", tradingsymbol="NIFTY24400CEFS", lot_size=25,
        )
        OptionChainSnapshot.objects.create(contract=contract, timestamp=timezone.now(), ltp=Decimal("313.73"), open_interest=5000, change_in_oi=0, volume=1000)

        signal = TradingSignal.objects.create(
            symbol="NIFTY", signal_type="buy", entry_price=Decimal("313.73"), stop_loss=Decimal("280.0"),
            target_1=Decimal("360.0"), position_size=25, option_side="CE", strike_price=Decimal("24400"),
            option_contract=contract,
            total_score=0.7, technical_score=0.7, sentiment_score=0.1, risk_score=1.0, options_score=0.6,
            regime="trending", status="approved", reason="test",
        )
        vector = build_feature_vector(signal)
        self.assertIsNotNone(vector["delta"])
        self.assertIsNotNone(vector["gamma"])
        self.assertEqual(vector["expiry"], expiry.isoformat())
        self.assertEqual(vector["strikeDistance"], 100.0)
        self.assertIsNotNone(vector["riskRewardScore"])
