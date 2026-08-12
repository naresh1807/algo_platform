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
from dataclasses import dataclass, field

from apps.market_data.indicators import indicator_dict_at, load_full_indicator_frame
from apps.market_data.regime import classify_regime
from apps.signals.engine import _evaluate_buy_conditions
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


@dataclass
class SimulatedTrade:
    entry_index: int
    entry_price: float
    stop_loss: float
    target_1: float
    exit_index: int
    exit_price: float
    exit_reason: str  # "stop_loss", "target_1", "time_stop"
    r_multiple: float


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    technical_score_threshold: float
    atr_stop_multiplier: float
    total_candles: int
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None
    expectancy_r: float | None = None
    trades: list[SimulatedTrade] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "technical_score_threshold": self.technical_score_threshold,
            "atr_stop_multiplier": self.atr_stop_multiplier,
            "total_candles": self.total_candles, "total_trades": self.total_trades,
            "wins": self.wins, "losses": self.losses,
            "win_rate": self.win_rate, "profit_factor": self.profit_factor,
            "expectancy_r": self.expectancy_r,
        }


def _simulate_exit(df, entry_index: int, entry_price: float, stop_loss: float,
                    target_1: float, max_holding_bars: int) -> SimulatedTrade:
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
    last_index = min(entry_index + max_holding_bars, n - 1)
    for i in range(entry_index + 1, last_index + 1):
        low = float(df.iloc[i]["low"])
        high = float(df.iloc[i]["high"])
        if low <= stop_loss:
            return SimulatedTrade(
                entry_index=entry_index, entry_price=entry_price, stop_loss=stop_loss,
                target_1=target_1, exit_index=i, exit_price=stop_loss,
                exit_reason="stop_loss", r_multiple=-1.0,
            )
        if high >= target_1:
            return SimulatedTrade(
                entry_index=entry_index, entry_price=entry_price, stop_loss=stop_loss,
                target_1=target_1, exit_index=i, exit_price=target_1,
                exit_reason="target_1", r_multiple=1.0,
            )
    exit_price = float(df.iloc[last_index]["close"])
    risk = entry_price - stop_loss
    r_multiple = (exit_price - entry_price) / risk if risk > 0 else 0.0
    return SimulatedTrade(
        entry_index=entry_index, entry_price=entry_price, stop_loss=stop_loss,
        target_1=target_1, exit_index=last_index, exit_price=exit_price,
        exit_reason="time_stop", r_multiple=round(r_multiple, 4),
    )


def run_backtest(
    symbol: str, timeframe: str = "5m",
    technical_score_threshold: float = 0.7, atr_stop_multiplier: float = 1.5,
    max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
    start_index: int = 0, end_index: int | None = None,
) -> BacktestResult:
    """
    Single backtest run over one threshold combo. start_index/end_index
    let walk_forward_backtest() below restrict this to a chronological
    slice (train vs. test) of the same loaded frame without reloading
    data for every combo -- see that function for the actual walk-forward
    orchestration; call this directly only for a one-off, in-sample check.

    Skips a bar entirely (no signal, no rejection logged -- this is an
    offline analysis tool, not apps.signals.engine, so there is no
    TradingSignal row to write for a NO_TRADE) if regime is
    HIGH_VOLATILITY, mirroring generate_signal()'s own behavior exactly
    (a high-volatility bar unconditionally adds a rejection reason there
    -- see that function's regime check).
    """
    df = load_full_indicator_frame(symbol, timeframe)
    if df.empty:
        return BacktestResult(
            symbol=symbol, timeframe=timeframe,
            technical_score_threshold=technical_score_threshold,
            atr_stop_multiplier=atr_stop_multiplier, total_candles=0,
        )

    n = len(df)
    end_index = n - 1 if end_index is None else min(end_index, n - 1)
    start_index = max(start_index, 1)

    trades: list[SimulatedTrade] = []
    i = start_index
    while i <= end_index:
        ind = indicator_dict_at(df, i)
        regime = classify_regime(ind)
        if regime == MarketRegime.HIGH_VOLATILITY:
            i += 1
            continue

        conditions = _evaluate_buy_conditions(ind)
        if regime == MarketRegime.SIDEWAYS:
            conditions["rsi_above_regime_threshold"] = ind["rsi"] > 60
        technical_score = sum(conditions.values()) / len(conditions)

        if technical_score >= technical_score_threshold:
            entry_price = ind["close"]
            stop_loss = entry_price - ind["atr"] * atr_stop_multiplier
            target_1 = entry_price + (entry_price - stop_loss)
            if stop_loss < entry_price:  # guard a degenerate zero/negative-ATR bar
                trade = _simulate_exit(df, i, entry_price, stop_loss, target_1, max_holding_bars)
                trades.append(trade)
                i = trade.exit_index + 1  # no overlapping trades on this symbol
                continue
        i += 1

    result = BacktestResult(
        symbol=symbol, timeframe=timeframe,
        technical_score_threshold=technical_score_threshold,
        atr_stop_multiplier=atr_stop_multiplier,
        total_candles=end_index - start_index + 1, trades=trades,
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
        result.expectancy_r = round(sum(t.r_multiple for t in trades) / len(trades), 4)

    return result


def walk_forward_backtest(
    symbol: str, timeframe: str = "5m",
    technical_score_thresholds: list[float] | None = None,
    atr_stop_multipliers: list[float] | None = None,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    min_trades_to_rank: int = 5,
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
    technical_score_thresholds = technical_score_thresholds or [0.5, 0.6, 0.7, 0.8]
    atr_stop_multipliers = atr_stop_multipliers or [1.2, 1.5, 1.8]

    df = load_full_indicator_frame(symbol, timeframe)
    if df.empty:
        return {"error": "insufficient_historical_data", "symbol": symbol, "timeframe": timeframe}

    n = len(df)
    split_index = int(n * (1 - test_fraction))

    train_results = []
    for threshold, multiplier in itertools.product(technical_score_thresholds, atr_stop_multipliers):
        result = run_backtest(
            symbol, timeframe, technical_score_threshold=threshold,
            atr_stop_multiplier=multiplier, start_index=1, end_index=split_index - 1,
        )
        if result.total_trades >= min_trades_to_rank:
            train_results.append(result)

    if not train_results:
        return {
            "error": "no_combo_reached_min_trades_on_train_slice",
            "symbol": symbol, "timeframe": timeframe,
            "train_candles": split_index, "min_trades_to_rank": min_trades_to_rank,
        }

    train_results.sort(key=lambda r: (r.expectancy_r if r.expectancy_r is not None else -999), reverse=True)
    best = train_results[0]

    test_result = run_backtest(
        symbol, timeframe, technical_score_threshold=best.technical_score_threshold,
        atr_stop_multiplier=best.atr_stop_multiplier, start_index=split_index, end_index=n - 1,
    )

    return {
        "symbol": symbol, "timeframe": timeframe,
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
            "here, see this module's docstring."
        ),
    }
