"""
Walk-forward backtest engine for the TECHNICAL entry/exit layer only
(apps.signals.engine._evaluate_buy_conditions + apps.market_data.regime
+ an ATR-based stop/target) -- the piece the manual's section 19
backtesting standards were flagged as missing for in the README's
honest-gap notes.

SCOPE, STATED UP FRONT: this replays real HistoricalData candles through
the exact same technical-condition functions apps.signals.engine uses
live (so a backtested "matched 6/8 conditions" and a live one mean
identically the same thing), and simulates ATR-based stop-loss/target
exits bar-by-bar. It deliberately does NOT replay sentiment
(apps.news), options-chain confluence (apps.options), or the dynamic
risk-engine state (drawdown, daily loss limit, consecutive-loss
cooldown) -- there is no historical time-series of what the news
sentiment score or options chain actually looked like at each past
candle stored anywhere in this system, and fabricating one would make
the backtest look more complete than it honestly is. What this DOES
give an honest, real-data-backed answer to: "for this symbol/timeframe,
how would TECHNICAL_SCORE_THRESHOLD and ATR_STOP_MULTIPLIER have
performed on their own, historically" -- exactly the two soft
parameters apps.learning's daily review already nudges, so this is a
principled way to pick a starting point for them instead of guessing.

Chronological train/test split (never random), same walk-forward
principle apps.learning.ml_train already uses for the same reason: a
random split lets a threshold "see the future," inflating how good it
looks versus what it would have actually done trading forward in time.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from apps.market_data.indicators import indicator_dict_at, load_full_indicator_frame
from apps.market_data.regime import classify_regime
from apps.signals.engine import HIGH_VOLATILITY_SCORE_MARGIN, _evaluate_buy_conditions
from common.constants import MarketRegime

# Bars to hold a simulated trade before giving up and closing at market,
# if neither the stop nor target_1 was hit -- without this, a trade
# that never resolves would hang open for the rest of the backtest and
# silently block every later entry on that symbol (this engine assumes
# one open trade per symbol at a time, same as apps.execution.paper_executor).
DEFAULT_MAX_HOLDING_BARS = 48

# Fraction of the date-ordered candle range held out as the
# out-of-sample "test" window when walk_forward_backtest() picks a
# threshold combo on the "train" window -- same HOLDOUT_FRACTION
# convention apps.learning.ml_train already uses, for the same reason.
DEFAULT_TEST_FRACTION = 0.3

# Conservative, transparent friction assumptions for the liquid underlying
# price series this technical-layer backtest replays. Both figures are per
# side, so the default models 20 bps of round-trip drag in total (10 bps from
# adverse fills and 10 bps from brokerage/taxes/exchange charges). They are
# invocation-level inputs rather than estimates inferred from future spreads
# or volumes, which the stored candle data cannot truthfully provide. Pass
# both values as 0 for an explicit frictionless comparison run.
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 5.0
DEFAULT_FEES_BPS_PER_SIDE = 5.0


def _validate_cost_assumptions(slippage_bps_per_side: float, fees_bps_per_side: float) -> None:
    """Reject nonsensical cost inputs before they can distort a report."""
    for label, value in (
        ("slippage_bps_per_side", slippage_bps_per_side),
        ("fees_bps_per_side", fees_bps_per_side),
    ):
        if not math.isfinite(value) or value < 0 or value >= 10_000:
            raise ValueError(f"{label} must be finite and in the range 0 <= value < 10000.")


def _cost_model_dict(slippage_bps_per_side: float, fees_bps_per_side: float) -> dict:
    """Serializable cost disclosure shared by CLI and programmatic reports."""
    return {
        "slippage_bps_per_side": slippage_bps_per_side,
        "fees_bps_per_side": fees_bps_per_side,
        "assumed_round_trip_drag_bps": 2 * (slippage_bps_per_side + fees_bps_per_side),
        "metrics_are_net_of_costs": True,
    }


@dataclass
class SimulatedTrade:
    entry_index: int
    entry_price: float
    stop_loss: float
    target_1: float
    exit_index: int
    exit_price: float
    exit_reason: str  # "stop_loss", "target_1", "time_stop"
    gross_r_multiple: float
    entry_fill_price: float
    exit_fill_price: float
    fees_price_units: float
    cost_r: float
    r_multiple: float


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    technical_score_threshold: float
    atr_stop_multiplier: float
    total_candles: int
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE
    fees_bps_per_side: float = DEFAULT_FEES_BPS_PER_SIDE
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None
    gross_expectancy_r: float | None = None
    expectancy_r: float | None = None
    average_cost_r: float | None = None
    total_fees_price_units: float = 0.0
    trades: list[SimulatedTrade] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "technical_score_threshold": self.technical_score_threshold,
            "atr_stop_multiplier": self.atr_stop_multiplier,
            "total_candles": self.total_candles, "total_trades": self.total_trades,
            "wins": self.wins, "losses": self.losses,
            "win_rate": self.win_rate, "profit_factor": self.profit_factor,
            "gross_expectancy_r": self.gross_expectancy_r,
            "expectancy_r": self.expectancy_r,
            "average_cost_r": self.average_cost_r,
            "total_fees_price_units": self.total_fees_price_units,
            "cost_model": _cost_model_dict(
                self.slippage_bps_per_side, self.fees_bps_per_side,
            ),
        }


def _trade_with_costs(
    *,
    entry_index: int,
    entry_price: float,
    stop_loss: float,
    target_1: float,
    exit_index: int,
    exit_price: float,
    exit_reason: str,
    slippage_bps_per_side: float,
    fees_bps_per_side: float,
) -> SimulatedTrade:
    """
    Convert a reference-price outcome into conservative long-side fills.

    The entry is slipped upward and the exit downward. Fees are charged on
    both sides' executed turnover. Inputs are either fixed before the run or
    come from the exit bar currently being processed; no future bar, spread,
    or volume is consulted to choose a cost after its outcome is known.
    """
    _validate_cost_assumptions(slippage_bps_per_side, fees_bps_per_side)
    risk = entry_price - stop_loss
    gross_r = (exit_price - entry_price) / risk if risk > 0 else 0.0

    slippage_rate = slippage_bps_per_side / 10_000
    fee_rate = fees_bps_per_side / 10_000
    entry_fill = entry_price * (1 + slippage_rate)
    exit_fill = exit_price * (1 - slippage_rate)
    fees = (entry_fill + exit_fill) * fee_rate
    net_r = (exit_fill - entry_fill - fees) / risk if risk > 0 else 0.0

    return SimulatedTrade(
        entry_index=entry_index,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_1=target_1,
        exit_index=exit_index,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_r_multiple=round(gross_r, 4),
        entry_fill_price=round(entry_fill, 6),
        exit_fill_price=round(exit_fill, 6),
        fees_price_units=round(fees, 6),
        cost_r=round(gross_r - net_r, 4),
        r_multiple=round(net_r, 4),
    )


def _simulate_exit(
    df,
    entry_index: int,
    entry_price: float,
    stop_loss: float,
    target_1: float,
    max_holding_bars: int,
    end_index: int | None = None,
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    fees_bps_per_side: float = DEFAULT_FEES_BPS_PER_SIDE,
) -> SimulatedTrade:
    """
    Walks forward bar-by-bar from entry_index+1, checking each candle's
    high/low against target_1/stop_loss. Stop-loss is checked before
    target on any bar where both could theoretically have been touched
    (a candle's low <= stop AND high >= target) -- this is the
    conservative assumption backtests should default to absent
    intra-candle tick data to know which was actually hit first;
    assuming the better outcome would flatter the results dishonestly.
    """
    n = len(df)
    # A train/test slice is a hard information boundary, not merely a bound on
    # where entries may be opened.  Without this cap, a trade opened near the
    # end of a training slice could resolve on a candle from the holdout slice,
    # leaking the very future data the chronological split is meant to protect.
    slice_end = n - 1 if end_index is None else min(end_index, n - 1)
    last_index = min(entry_index + max_holding_bars, slice_end)
    if last_index <= entry_index:
        raise ValueError("A simulated trade needs at least one post-entry candle inside its slice.")
    for i in range(entry_index + 1, last_index + 1):
        low = float(df.iloc[i]["low"])
        high = float(df.iloc[i]["high"])
        if low <= stop_loss:
            return _trade_with_costs(
                entry_index=entry_index, entry_price=entry_price, stop_loss=stop_loss,
                target_1=target_1, exit_index=i, exit_price=stop_loss,
                exit_reason="stop_loss", slippage_bps_per_side=slippage_bps_per_side,
                fees_bps_per_side=fees_bps_per_side,
            )
        if high >= target_1:
            return _trade_with_costs(
                entry_index=entry_index, entry_price=entry_price, stop_loss=stop_loss,
                target_1=target_1, exit_index=i, exit_price=target_1,
                exit_reason="target_1", slippage_bps_per_side=slippage_bps_per_side,
                fees_bps_per_side=fees_bps_per_side,
            )
    exit_price = float(df.iloc[last_index]["close"])
    return _trade_with_costs(
        entry_index=entry_index, entry_price=entry_price, stop_loss=stop_loss,
        target_1=target_1, exit_index=last_index, exit_price=exit_price,
        exit_reason="time_stop", slippage_bps_per_side=slippage_bps_per_side,
        fees_bps_per_side=fees_bps_per_side,
    )


def run_backtest(
    symbol: str, timeframe: str = "5m",
    technical_score_threshold: float = 0.7, atr_stop_multiplier: float = 1.5,
    max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
    start_index: int = 0, end_index: int | None = None,
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    fees_bps_per_side: float = DEFAULT_FEES_BPS_PER_SIDE,
) -> BacktestResult:
    """
    Single backtest run over one threshold combo. start_index/end_index
    let walk_forward_backtest() below restrict this to a chronological
    slice (train vs. test) of the same loaded frame without reloading
    data for every combo -- see that function for the actual walk-forward
    orchestration; call this directly only for a one-off, in-sample check.

    High-volatility bars are not skipped -- mirroring generate_signal()'s
    own behavior exactly (manual section 12: "stricter filters AND
    smaller size", not an outright ban), the technical_score_threshold
    is raised by HIGH_VOLATILITY_SCORE_MARGIN for that bar only, so a
    genuinely strong setup can still trade there, just held to a higher
    bar (sizing itself isn't simulated here, see this module's docstring).
    """
    _validate_cost_assumptions(slippage_bps_per_side, fees_bps_per_side)
    df = load_full_indicator_frame(symbol, timeframe)
    if df.empty:
        return BacktestResult(
            symbol=symbol, timeframe=timeframe,
            technical_score_threshold=technical_score_threshold,
            atr_stop_multiplier=atr_stop_multiplier, total_candles=0,
            slippage_bps_per_side=slippage_bps_per_side,
            fees_bps_per_side=fees_bps_per_side,
        )

    n = len(df)
    end_index = n - 1 if end_index is None else min(end_index, n - 1)
    start_index = max(start_index, 1)

    trades: list[SimulatedTrade] = []
    i = start_index
    # The final candle in a slice cannot be a new entry: there is no later
    # in-slice candle on which its outcome can be known.  Skipping it is the
    # honest alternative to either fabricating a same-bar exit or looking into
    # the next (possibly held-out) slice.
    while i < end_index:
        ind = indicator_dict_at(df, i)
        regime = classify_regime(ind)

        conditions = _evaluate_buy_conditions(ind)
        if regime == MarketRegime.SIDEWAYS:
            conditions["rsi_above_regime_threshold"] = ind["rsi"] > 60
        technical_score = sum(conditions.values()) / len(conditions)

        effective_score_threshold = technical_score_threshold
        if regime == MarketRegime.HIGH_VOLATILITY:
            effective_score_threshold = min(1.0, technical_score_threshold + HIGH_VOLATILITY_SCORE_MARGIN)

        if technical_score >= effective_score_threshold:
            entry_price = ind["close"]
            stop_loss = entry_price - ind["atr"] * atr_stop_multiplier
            target_1 = entry_price + (entry_price - stop_loss)
            if stop_loss < entry_price:  # guard a degenerate zero/negative-ATR bar
                trade = _simulate_exit(
                    df,
                    i,
                    entry_price,
                    stop_loss,
                    target_1,
                    max_holding_bars,
                    end_index=end_index,
                    slippage_bps_per_side=slippage_bps_per_side,
                    fees_bps_per_side=fees_bps_per_side,
                )
                trades.append(trade)
                i = trade.exit_index + 1  # no overlapping trades on this symbol
                continue
        i += 1

    result = BacktestResult(
        symbol=symbol, timeframe=timeframe,
        technical_score_threshold=technical_score_threshold,
        atr_stop_multiplier=atr_stop_multiplier,
        total_candles=end_index - start_index + 1,
        slippage_bps_per_side=slippage_bps_per_side,
        fees_bps_per_side=fees_bps_per_side,
        trades=trades,
    )
    result.total_trades = len(trades)
    if trades:
        wins = [t for t in trades if t.r_multiple > 0]
        losses = [t for t in trades if t.r_multiple <= 0]
        result.wins = len(wins)
        result.losses = len(losses)
        result.win_rate = round(len(wins) / len(trades), 4)
        gross_win = sum(t.r_multiple for t in wins)
        gross_loss = abs(sum(t.r_multiple for t in losses))
        result.profit_factor = round(gross_win / gross_loss, 4) if gross_loss > 0 else None
        result.gross_expectancy_r = round(sum(t.gross_r_multiple for t in trades) / len(trades), 4)
        result.expectancy_r = round(sum(t.r_multiple for t in trades) / len(trades), 4)
        result.average_cost_r = round(sum(t.cost_r for t in trades) / len(trades), 4)
        result.total_fees_price_units = round(sum(t.fees_price_units for t in trades), 6)

    return result


def walk_forward_backtest(
    symbol: str, timeframe: str = "5m",
    technical_score_thresholds: list[float] | None = None,
    atr_stop_multipliers: list[float] | None = None,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    min_trades_to_rank: int = 5,
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    fees_bps_per_side: float = DEFAULT_FEES_BPS_PER_SIDE,
) -> dict:
    """
    The actual recommended entry point: grid-searches every combo of
    technical_score_thresholds x atr_stop_multipliers on the
    chronologically EARLIER (1 - test_fraction) slice of history
    ("train"), ranks combos by expectancy_r on that slice, then reports
    how the single best train-slice combo performs on the LATER,
    untouched slice ("test") -- the number that should actually inform
    a decision, since it's the closest available proxy to "how would
    this have done on data the selection process never saw."

    A combo with fewer than min_trades_to_rank trades on the train slice
    is excluded from ranking (not scored as 0) -- an expectancy computed
    from a handful of trades is mostly noise and would otherwise let a
    lucky-but-thin combo win the grid search.
    """
    _validate_cost_assumptions(slippage_bps_per_side, fees_bps_per_side)
    cost_model = _cost_model_dict(slippage_bps_per_side, fees_bps_per_side)
    technical_score_thresholds = technical_score_thresholds or [0.5, 0.6, 0.7, 0.8]
    atr_stop_multipliers = atr_stop_multipliers or [1.2, 1.5, 1.8]

    df = load_full_indicator_frame(symbol, timeframe)
    if df.empty:
        return {
            "error": "insufficient_historical_data", "symbol": symbol,
            "timeframe": timeframe, "cost_model": cost_model,
        }

    n = len(df)
    split_index = int(n * (1 - test_fraction))

    train_results = []
    for threshold, multiplier in itertools.product(technical_score_thresholds, atr_stop_multipliers):
        result = run_backtest(
            symbol, timeframe, technical_score_threshold=threshold,
            atr_stop_multiplier=multiplier, start_index=1, end_index=split_index - 1,
            slippage_bps_per_side=slippage_bps_per_side,
            fees_bps_per_side=fees_bps_per_side,
        )
        if result.total_trades >= min_trades_to_rank:
            train_results.append(result)

    if not train_results:
        return {
            "error": "no_combo_reached_min_trades_on_train_slice",
            "symbol": symbol, "timeframe": timeframe,
            "train_candles": split_index, "min_trades_to_rank": min_trades_to_rank,
            "cost_model": cost_model,
        }

    train_results.sort(key=lambda r: (r.expectancy_r if r.expectancy_r is not None else -999), reverse=True)
    best = train_results[0]

    test_result = run_backtest(
        symbol, timeframe, technical_score_threshold=best.technical_score_threshold,
        atr_stop_multiplier=best.atr_stop_multiplier, start_index=split_index, end_index=n - 1,
        slippage_bps_per_side=slippage_bps_per_side,
        fees_bps_per_side=fees_bps_per_side,
    )

    return {
        "symbol": symbol, "timeframe": timeframe, "cost_model": cost_model,
        "train_candles": split_index, "test_candles": n - split_index,
        "selected_technical_score_threshold": best.technical_score_threshold,
        "selected_atr_stop_multiplier": best.atr_stop_multiplier,
        "train_metrics": best.as_dict(),
        "test_metrics": test_result.as_dict(),
        "all_train_combos_tried": [r.as_dict() for r in train_results],
        "note": (
            "test_metrics is the out-of-sample number -- the honest estimate of how "
            "this combo would perform going forward. train_metrics is what the grid "
            "search selected on, and is optimistic by construction (same reason "
            "apps.learning.ml_train reports chronological holdout separately from "
            "train-set accuracy). Scope reminder: technical layer only -- sentiment, "
            "options-chain confluence, and live risk-engine state are not replayed "
            "here, see this module's docstring. All performance metrics are net of "
            "the disclosed static slippage and fee assumptions."
        ),
    }


def _aggregate_r_multiples(r_multiples: list[float]) -> dict:
    """
    Pool statistics over a list of trade R-multiples (e.g. every
    out-of-sample trade across every fold of rolling_walk_forward_
    backtest below) -- win_rate/profit_factor/expectancy_r match
    BacktestResult's own definitions; sharpe_r/sortino_r/calmar_r are
    the TRADE-SEQUENCE analogues of apps.analytics.services'
    equity-curve versions (mean/downside-deviation/drawdown computed
    over R-multiples directly, since a backtest has no daily equity
    curve of its own, only a sequence of trade outcomes) -- named with
    an "_r" suffix specifically so they're never confused with the
    equity-curve Sharpe/Sortino/Calmar apps.analytics.services reports
    for LIVE trading.
    """
    import statistics

    if not r_multiples:
        return {
            "trade_count": 0, "win_rate": None, "profit_factor": None, "expectancy_r": None,
            "sharpe_r": None, "sortino_r": None, "calmar_r": None, "max_drawdown_r": None,
        }

    wins = [r for r in r_multiples if r > 0]
    losses = [r for r in r_multiples if r <= 0]
    win_rate = round(len(wins) / len(r_multiples), 4)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = round(gross_win / gross_loss, 4) if gross_loss > 0 else None
    expectancy_r = round(sum(r_multiples) / len(r_multiples), 4)

    sharpe_r = None
    sortino_r = None
    if len(r_multiples) >= 2:
        mean_r = statistics.mean(r_multiples)
        stdev_r = statistics.pstdev(r_multiples)
        if stdev_r > 0:
            sharpe_r = round(mean_r / stdev_r, 4)
        downside = [r for r in r_multiples if r < 0]
        if downside:
            downside_dev = statistics.pstdev(downside) if len(downside) > 1 else abs(downside[0])
            if downside_dev > 0:
                sortino_r = round(mean_r / downside_dev, 4)

    # Max drawdown over the CUMULATIVE r-multiple curve -- treats each
    # trade as adding r_multiple "R units" to a running total, the same
    # peak-to-trough measurement apps.analytics.services.
    # compute_max_drawdown_for_day uses for a real equity curve, just in
    # R-units instead of currency.
    cumulative = 0.0
    peak = 0.0
    max_dd_r = 0.0
    for r in r_multiples:
        cumulative += r
        peak = max(peak, cumulative)
        max_dd_r = max(max_dd_r, peak - cumulative)

    calmar_r = round(expectancy_r * len(r_multiples) / max_dd_r, 4) if max_dd_r > 0 else None

    return {
        "trade_count": len(r_multiples), "win_rate": win_rate, "profit_factor": profit_factor,
        "expectancy_r": expectancy_r, "sharpe_r": sharpe_r, "sortino_r": sortino_r,
        "calmar_r": calmar_r, "max_drawdown_r": round(max_dd_r, 4),
    }


def rolling_walk_forward_backtest(
    symbol: str, timeframe: str = "5m",
    technical_score_thresholds: list[float] | None = None,
    atr_stop_multipliers: list[float] | None = None,
    n_folds: int = 5,
    min_trades_to_rank: int = 5,
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    fees_bps_per_side: float = DEFAULT_FEES_BPS_PER_SIDE,
) -> dict:
    """
    A REAL rolling multi-fold walk-forward: Train -> Test -> Move the
    window forward -> Repeat -- distinct from walk_forward_backtest()
    above, which despite its name is a SINGLE train/test split (that
    function is unchanged and still available for a faster one-shot
    check; existing callers of it are unaffected by this addition).

    The candle range is divided into n_folds+1 equal-sized chronological
    segments. Fold i's TRAIN window is an EXPANDING window (every
    segment up to and including fold i) -- more history only ever helps
    expectancy estimation here, and NSE index data doesn't have the
    kind of regime non-stationarity (e.g. a stock post-restructuring)
    that would make discarding older data actively necessary. Fold i's
    TEST window is the segment immediately after, strictly later in
    time than anything the fold's own combo-selection ever saw -- no
    future information leaks into a fold's own selection, the same
    anti-look-ahead discipline walk_forward_backtest already applies to
    its own single split.

    Returns per-fold results PLUS an aggregate_out_of_sample_metrics
    dict pooling every fold's TEST-window trades together -- that pooled
    number, not any single fold's, is the one that should actually
    inform a decision: it's the closest available proxy to "how would
    this selection PROCESS have performed, repeated over time," which
    is a stronger claim than any one fold's often-noisy result alone.
    """
    _validate_cost_assumptions(slippage_bps_per_side, fees_bps_per_side)
    cost_model = _cost_model_dict(slippage_bps_per_side, fees_bps_per_side)
    technical_score_thresholds = technical_score_thresholds or [0.5, 0.6, 0.7, 0.8]
    atr_stop_multipliers = atr_stop_multipliers or [1.2, 1.5, 1.8]

    if n_folds < 2:
        raise ValueError("n_folds must be >= 2 -- need at least one train segment and one test segment.")

    df = load_full_indicator_frame(symbol, timeframe)
    if df.empty:
        return {
            "error": "insufficient_historical_data", "symbol": symbol,
            "timeframe": timeframe, "cost_model": cost_model,
        }

    n = len(df)
    segment_size = n // (n_folds + 1)
    if segment_size < 10:
        return {
            "error": "insufficient_candles_for_requested_fold_count",
            "symbol": symbol, "timeframe": timeframe, "total_candles": n,
            "n_folds": n_folds, "cost_model": cost_model,
        }

    fold_results = []
    all_test_r_multiples: list[float] = []
    all_test_gross_r_multiples: list[float] = []
    all_test_cost_r: list[float] = []

    for fold in range(n_folds):
        train_end = segment_size * (fold + 1)
        test_start = train_end
        test_end = min(segment_size * (fold + 2), n) - 1

        train_candidates = []
        for threshold, multiplier in itertools.product(technical_score_thresholds, atr_stop_multipliers):
            result = run_backtest(
                symbol, timeframe, technical_score_threshold=threshold,
                atr_stop_multiplier=multiplier, start_index=1, end_index=train_end - 1,
                slippage_bps_per_side=slippage_bps_per_side,
                fees_bps_per_side=fees_bps_per_side,
            )
            if result.total_trades >= min_trades_to_rank:
                train_candidates.append(result)

        if not train_candidates:
            fold_results.append({
                "fold": fold, "error": "no_combo_reached_min_trades_on_train_window", "train_candles": train_end,
            })
            continue

        train_candidates.sort(key=lambda r: (r.expectancy_r if r.expectancy_r is not None else -999), reverse=True)
        best = train_candidates[0]

        test_result = run_backtest(
            symbol, timeframe, technical_score_threshold=best.technical_score_threshold,
            atr_stop_multiplier=best.atr_stop_multiplier, start_index=test_start, end_index=test_end,
            slippage_bps_per_side=slippage_bps_per_side,
            fees_bps_per_side=fees_bps_per_side,
        )
        all_test_r_multiples.extend(t.r_multiple for t in test_result.trades)
        all_test_gross_r_multiples.extend(t.gross_r_multiple for t in test_result.trades)
        all_test_cost_r.extend(t.cost_r for t in test_result.trades)

        fold_results.append({
            "fold": fold, "train_candles": train_end,
            "test_start_index": test_start, "test_end_index": test_end,
            "selected_technical_score_threshold": best.technical_score_threshold,
            "selected_atr_stop_multiplier": best.atr_stop_multiplier,
            "train_metrics": best.as_dict(), "test_metrics": test_result.as_dict(),
        })

    valid_folds = [f for f in fold_results if "error" not in f]
    if not valid_folds:
        return {
            "error": "no_fold_produced_a_valid_result",
            "symbol": symbol, "timeframe": timeframe, "fold_results": fold_results,
            "cost_model": cost_model,
        }

    aggregate_metrics = _aggregate_r_multiples(all_test_r_multiples)
    gross_aggregate_metrics = _aggregate_r_multiples(all_test_gross_r_multiples)
    aggregate_metrics.update({
        "gross_expectancy_r": gross_aggregate_metrics["expectancy_r"],
        "average_cost_r": (
            round(sum(all_test_cost_r) / len(all_test_cost_r), 4)
            if all_test_cost_r else None
        ),
        "metrics_are_net_of_costs": True,
    })

    return {
        "symbol": symbol, "timeframe": timeframe, "n_folds": n_folds,
        "total_candles": n, "cost_model": cost_model,
        "fold_results": fold_results,
        "aggregate_out_of_sample_metrics": aggregate_metrics,
        "note": (
            "aggregate_out_of_sample_metrics pools every fold's TEST-window trades -- "
            "this is the number that should actually inform a decision, not any single "
            "fold's own result. Same scope reminder as walk_forward_backtest: technical "
            "layer only, sentiment/options-chain/live-risk-engine state not replayed. "
            "All performance metrics are net of the disclosed static slippage and fee "
            "assumptions."
        ),
    }
