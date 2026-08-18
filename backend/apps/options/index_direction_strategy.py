"""
Index-direction CE/PE strategy: "if NIFTY/BANKNIFTY is moving up, check
the call side's historical success rate; if moving down, check the put
side's; take the trade only if that side has actually been profitable."

This is deliberately a SEPARATE strategy from apps.signals.engine.
generate_signal() (the general per-symbol technical engine), not a
replacement for it -- generate_signal() only ever produces BUY/long
setups and has no notion of "which option side," while this module
exists specifically to make the CE-vs-PE call apps.options' analytics
(signals_engine.options_confluence_score, strike_selector.
suggest_best_strike) already support but nothing wired into a decision
to actually trade before now.

SCOPE, STATED UP FRONT (same honesty convention as apps.analytics.
backtest's own docstring):
  - Both PAPER and LIVE mode trade the REAL suggested option contract
    (via strike_selector.suggest_best_strike -> the resolved
    OptionContract), priced/sized in that contract's own premium (see
    the entry_price/stop_loss/target computation below, and
    TradingSignal.option_contract's own docstring) -- not the
    underlying-index proxy this module used to trade unconditionally.
    If no contract can be resolved (no options data synced yet, no
    strike in the delta sweet spot, liquidity gate failed, position
    size rounds down to under one lot, ...), the signal is NO_TRADE;
    there is no fallback to trading the underlying instead.
    apps.execution.live_executor places a real NFO order using the
    resolved contract's own symbol_token/tradingsymbol -- see that
    module's docstring, and its own pre-real-account safety caveats,
    before ever setting BROKER_MODE=live for this strategy.
  - The "success rate" is bootstrapped by backtesting the direction's
    technical conditions against real historical index candles (see
    _simulate_directional_r_multiples) -- same technical-layer-only
    scope apps.analytics.backtest documents for its own bullish-only
    backtest (no historical options/sentiment/risk-engine time series
    exists to replay). Once this strategy has actually closed enough of
    its own real trades, a later revision should prefer that real
    history over the backtest proxy, the same way apps.learning already
    prefers real ml_train data over rule-based scores once enough
    exists -- not implemented yet since this strategy hasn't traded.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings

from apps.market_data.indicators import compute_indicators, indicator_dict_at, load_full_indicator_frame
from apps.market_data.regime import classify_regime, regime_size_multiplier
from apps.news.scoring import aggregate_sentiment
from apps.risk.engine import check_option_contract_liquidity, check_pre_trade
from apps.signals.engine import (
    ATR_STOP_MULTIPLIER,
    HIGH_VOLATILITY_SCORE_MARGIN,
    OPTIONS_SCORE_THRESHOLD,
    _create_signal,
    _evaluate_bearish_conditions,
    _evaluate_buy_conditions,
    _indicator_signal_fields,
)
from apps.signals.models import TradingSignal
from common.constants import MarketRegime, SignalStatus, SignalType

from .data_quality import validate_option_chain_snapshot
from .models import OptionContract
from .signals_engine import options_confluence_score, select_expiry
from .strike_selector import suggest_best_strike

# Reads settings.OPTIONS_PIPELINE_UNDERLYINGS (not hard-coded, and not
# imported by apps.signals.tasks -- that module reads the same setting
# independently, so neither app has to import the other's module to
# agree on which underlyings trade via real option contracts instead
# of apps.signals.engine's plain index-level engine).
INDEX_UNDERLYINGS = tuple(settings.OPTIONS_PIPELINE_UNDERLYINGS)

DIRECTION_TIMEFRAME = "5m"

# Same role as apps.signals.engine.TECHNICAL_SCORE_THRESHOLD, kept as
# an independent constant (not imported from there) since this
# strategy's bullish/bearish scoring is symmetric by construction and
# hasn't had its own backtest-driven tuning pass yet -- 0.7 matches
# apps.analytics.backtest.run_backtest's own default, a reasonable
# starting point pending real data.
DIRECTION_SCORE_THRESHOLD = 0.7

# A backtest bootstrap with fewer trades than this is mostly noise --
# same min_trades_to_rank convention apps.analytics.backtest.
# walk_forward_backtest uses for the identical reason.
MIN_BOOTSTRAP_TRADES = 5

# "Success rate" gate: win rate must clear this AND expectancy must be
# positive (see success_rate_for_side) -- a >50% win rate with negative
# expectancy (small wins, large losses) is not actually profitable, and
# a <50% win rate can still be profitable with a rich reward:risk, but
# the user's framing was explicitly "success rate ... take trade if
# profitable" -- both conditions, not just one.
SUCCESS_RATE_THRESHOLD = 0.5

# Bars to hold a simulated bootstrap trade before closing at market if
# neither stop nor target was hit -- same value and reasoning as
# apps.analytics.backtest.DEFAULT_MAX_HOLDING_BARS.
BOOTSTRAP_MAX_HOLDING_BARS = 48


def determine_index_direction(underlying: str, timeframe: str = DIRECTION_TIMEFRAME) -> dict:
    """
    Scores the current bar against BOTH apps.signals.engine's bullish
    conditions and their bearish mirror (_evaluate_bearish_conditions)
    -- same multi-confirmation rigor the equity engine already applies
    to its one-directional case. "Moving up"/"moving down" means a
    clear majority of the same eight technical conditions agree, not a
    single indicator.

    Returns {"direction": "up"|"down"|None, "option_side": "CE"|"PE"|None,
    "score": float, "regime": str|None, "ind": dict|None, "detail": str}.
    direction is None when neither side clears the threshold, or both
    do and it's a near-tie -- ambiguous is not a call worth trading.
    """
    ind = compute_indicators(underlying, timeframe)
    if ind is None:
        return {
            "direction": None, "option_side": None, "score": 0.0,
            "regime": None, "ind": None,
            "detail": f"Not enough historical candles for {underlying} yet.",
        }

    regime = classify_regime(ind)
    bullish = _evaluate_buy_conditions(ind)
    bearish = _evaluate_bearish_conditions(ind)
    if regime == MarketRegime.SIDEWAYS:
        bullish["rsi_above_regime_threshold"] = ind["rsi"] > 60
        bearish["rsi_below_regime_threshold"] = ind["rsi"] < 40

    bull_score = sum(bullish.values()) / len(bullish)
    bear_score = sum(bearish.values()) / len(bearish)

    threshold = DIRECTION_SCORE_THRESHOLD
    if regime == MarketRegime.HIGH_VOLATILITY:
        threshold = min(1.0, DIRECTION_SCORE_THRESHOLD + HIGH_VOLATILITY_SCORE_MARGIN)

    if bull_score >= threshold and bull_score > bear_score:
        return {
            "direction": "up", "option_side": "CE", "score": round(bull_score, 4),
            "regime": regime, "ind": ind,
            "detail": (
                f"{underlying} matched {sum(bullish.values())}/{len(bullish)} bullish "
                f"conditions ({bull_score:.0%}) -- moving up, call side."
            ),
        }
    if bear_score >= threshold and bear_score > bull_score:
        return {
            "direction": "down", "option_side": "PE", "score": round(bear_score, 4),
            "regime": regime, "ind": ind,
            "detail": (
                f"{underlying} matched {sum(bearish.values())}/{len(bearish)} bearish "
                f"conditions ({bear_score:.0%}) -- moving down, put side."
            ),
        }
    return {
        "direction": None, "option_side": None,
        "score": round(max(bull_score, bear_score), 4), "regime": regime, "ind": ind,
        "detail": (
            f"{underlying} has no clear directional bias right now "
            f"(bullish {bull_score:.0%}, bearish {bear_score:.0%})."
        ),
    }


def _simulate_directional_exit(
    df, entry_index: int, entry_price: float, stop: float, target: float,
    direction: str, max_holding_bars: int,
) -> tuple[int, float]:
    """
    Direction-aware sibling of apps.analytics.backtest._simulate_exit:
    for "up" this is identical to that function (profit as price rises
    to target, loss as it falls to stop); "down" mirrors both. Stop is
    checked before target on any bar where both could theoretically
    have been touched -- same conservative assumption, absent intra-
    candle tick data to know which was actually hit first.
    """
    n = len(df)
    last_index = min(entry_index + max_holding_bars, n - 1)
    for i in range(entry_index + 1, last_index + 1):
        low = float(df.iloc[i]["low"])
        high = float(df.iloc[i]["high"])
        if direction == "up":
            if low <= stop:
                return i, -1.0
            if high >= target:
                return i, 1.0
        else:
            if high >= stop:
                return i, -1.0
            if low <= target:
                return i, 1.0

    exit_price = float(df.iloc[last_index]["close"])
    risk = abs(entry_price - stop)
    # NaN-safe: written as "proceed only if risk is a real positive
    # number" rather than "skip if risk <= 0" -- every comparison
    # against NaN (a real possibility from a data gap in the underlying
    # candles) is False in Python, so `risk <= 0` would silently let a
    # NaN risk through unguarded, producing a NaN r_multiple that then
    # poisons sum(r_multiples) in _simulate_directional_r_multiples'
    # caller (success_rate_for_side's expectancy_r) for the WHOLE
    # backtest sample, not just this one trade. This exact shape of bug
    # shipped once already (see that guard's own comment).
    if not (risk > 0):
        return last_index, 0.0
    r_multiple = (
        (exit_price - entry_price) / risk if direction == "up"
        else (entry_price - exit_price) / risk
    )
    return last_index, round(r_multiple, 4)


def _simulate_directional_r_multiples(
    df, direction: str, atr_stop_multiplier: float = ATR_STOP_MULTIPLIER,
    max_holding_bars: int = BOOTSTRAP_MAX_HOLDING_BARS,
) -> list[float]:
    """
    Replays `df` (an apps.market_data.indicators.load_full_indicator_frame
    result) bar-by-bar, opening a simulated trade every time the
    direction's technical conditions clear DIRECTION_SCORE_THRESHOLD,
    and walking forward to stop/target/time-stop -- same mechanics as
    apps.analytics.backtest.run_backtest, generalized to also cover the
    bearish/PE case that module doesn't (see this module's own
    docstring for the shared scope limitation: this is a technical-
    layer, underlying-price proxy, not a real option premium replay).
    """
    r_multiples: list[float] = []
    n = len(df)
    i = 1
    while i < n:
        ind = indicator_dict_at(df, i)
        regime = classify_regime(ind)
        if regime == MarketRegime.HIGH_VOLATILITY:
            i += 1
            continue

        if direction == "up":
            conditions = _evaluate_buy_conditions(ind)
            if regime == MarketRegime.SIDEWAYS:
                conditions["rsi_above_regime_threshold"] = ind["rsi"] > 60
        else:
            conditions = _evaluate_bearish_conditions(ind)
            if regime == MarketRegime.SIDEWAYS:
                conditions["rsi_below_regime_threshold"] = ind["rsi"] < 40
        score = sum(conditions.values()) / len(conditions)

        if score < DIRECTION_SCORE_THRESHOLD:
            i += 1
            continue

        entry = ind["close"]
        atr_distance = ind["atr"] * atr_stop_multiplier
        # NaN-safe (see _simulate_directional_exit's identical-shape
        # guard above for why "proceed only if positive" instead of
        # "skip if <= 0" matters here): a NaN ATR bar -- e.g. from a
        # gap in the underlying candles -- must not silently pass this
        # guard and go on to poison this bar's r_multiple.
        if not (atr_distance > 0):
            i += 1
            continue
        stop = entry - atr_distance if direction == "up" else entry + atr_distance
        target = entry + atr_distance if direction == "up" else entry - atr_distance

        exit_index, r_multiple = _simulate_directional_exit(
            df, i, entry, stop, target, direction, max_holding_bars,
        )
        r_multiples.append(r_multiple)
        i = exit_index + 1  # no overlapping trades, same convention as analytics.backtest

    return r_multiples


def success_rate_for_side(underlying: str, direction: str, timeframe: str = DIRECTION_TIMEFRAME) -> dict:
    """
    Bootstraps a win-rate/expectancy estimate for one (underlying,
    direction) combo -- see _simulate_directional_r_multiples and this
    module's docstring for exactly what this proxy does and doesn't
    capture. `profitable` requires BOTH win_rate > SUCCESS_RATE_THRESHOLD
    AND positive expectancy_r, per the module-level constant's docstring.
    """
    side = "CE" if direction == "up" else "PE"
    df = load_full_indicator_frame(underlying, timeframe)
    if df.empty:
        return {
            "available": False, "trade_count": 0, "win_rate": None,
            "expectancy_r": None, "profit_factor": None, "profitable": False,
            "detail": f"No historical candles for {underlying} yet to bootstrap a {side}-side success rate.",
        }

    r_multiples = _simulate_directional_r_multiples(df, direction)
    count = len(r_multiples)
    if count < MIN_BOOTSTRAP_TRADES:
        return {
            "available": False, "trade_count": count, "win_rate": None,
            "expectancy_r": None, "profit_factor": None, "profitable": False,
            "detail": (
                f"Only {count} historical {side}-side setup(s) found in the backtest bootstrap "
                f"-- need >= {MIN_BOOTSTRAP_TRADES} to estimate a success rate."
            ),
        }

    wins = [r for r in r_multiples if r > 0]
    losses = [r for r in r_multiples if r <= 0]
    win_rate = round(len(wins) / count, 4)
    expectancy_r = round(sum(r_multiples) / count, 4)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = round(gross_win / gross_loss, 4) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    profitable = win_rate > SUCCESS_RATE_THRESHOLD and expectancy_r > 0

    pf_detail = f", profit factor {profit_factor:.2f}" if gross_loss > 0 else ""
    return {
        "available": True, "trade_count": count, "win_rate": win_rate,
        "expectancy_r": expectancy_r, "profit_factor": profit_factor, "profitable": profitable,
        "detail": (
            f"Backtest bootstrap over {count} historical {side}-side setups: "
            f"win rate {win_rate:.0%}, expectancy {expectancy_r:+.2f}R{pf_detail}."
        ),
    }


def evaluate_index_direction_trade(underlying: str, timeframe: str = DIRECTION_TIMEFRAME) -> TradingSignal:
    """
    The strategy's single entry point -- always returns a saved
    TradingSignal, never None, same "every decision is itself logged"
    discipline apps.signals.engine.generate_signal() follows.

    Pipeline: direction (determine_index_direction) -> that side's
    backtest-bootstrapped success rate (success_rate_for_side) ->
    sentiment veto -> options-chain confluence -> best-strike suggestion,
    which must resolve a real OptionContract (see module docstring -- no
    contract means NO_TRADE now, no underlying-proxy fallback) -> risk
    engine, sized against the option's own premium -> ML win-probability
    veto/sizing, rounded to whole lots. Any stage after direction
    detection can add a rejection reason; ANY reason means NO_TRADE,
    mirroring generate_signal()'s "run every check, report everything
    wrong" style.

    signal_type is always BUY once a contract is resolved -- buying a CE
    or a PE is always a LONG bet on THAT option's own premium (the
    up-vs-down call only decides which side, CE or PE, gets bought), not
    a BUY/SELL on the underlying the way the old underlying-proxy version
    of this function used to encode direction.
    """
    direction_result = determine_index_direction(underlying, timeframe)
    direction = direction_result["direction"]
    ind = direction_result["ind"]

    if direction is None:
        entry_price = Decimal(str(ind["close"])) if ind else Decimal("0")
        return _create_signal(
            symbol=underlying, signal_type=SignalType.NO_TRADE,
            entry_price=entry_price, stop_loss=entry_price,
            total_score=direction_result["score"], technical_score=direction_result["score"],
            sentiment_score=0, risk_score=0, options_score=0,
            regime=direction_result["regime"] or MarketRegime.SIDEWAYS,
            status=SignalStatus.REJECTED,
            rejection_stage=TradingSignal.RejectionStage.DIRECTION,
            reason=direction_result["detail"],
            **_indicator_signal_fields(ind),
        )

    side = direction_result["option_side"]
    option_direction = "bullish" if direction == "up" else "bearish"
    regime = direction_result["regime"]
    technical_score = direction_result["score"]

    reasons: list[str] = []
    # Tracks the FIRST pipeline stage that added a reason -- since
    # success_rate/sentiment below can both add to `reasons` before the
    # first `if not reasons:` gate further down, "first to add" (not
    # "last check that ran") is what actually explains why this signal
    # died, so every reasons.append() below is paired with
    # `stage = stage or "..."` (never overwrites an earlier stage).
    stage: str | None = None

    success = success_rate_for_side(underlying, direction, timeframe)
    if not success["available"]:
        reasons.append(success["detail"])
        stage = stage or TradingSignal.RejectionStage.SUCCESS_RATE
    elif not success["profitable"]:
        reasons.append(
            f"{side}-side historical success rate isn't profitable enough to trade: "
            f"{success['detail']} (needs win rate > {SUCCESS_RATE_THRESHOLD:.0%} and positive expectancy)."
        )
        stage = stage or TradingSignal.RejectionStage.SUCCESS_RATE

    sentiment = aggregate_sentiment(underlying)
    # Direction-aware, unlike apps.signals.engine (which only ever
    # evaluates a bullish case): a strongly NEGATIVE headline
    # contradicts the up/CE thesis, a strongly POSITIVE one contradicts
    # the down/PE thesis -- checking has_contradictory_headline
    # unconditionally for both directions would veto a PE trade on the
    # exact bearish news that actually confirms it.
    if direction == "up" and sentiment["has_contradictory_headline"]:
        reasons.append(
            "A strongly negative, high-confidence headline vetoes this CE setup "
            "(sentiment is a filter, not a standalone trigger -- but it CAN block)."
        )
        stage = stage or TradingSignal.RejectionStage.SENTIMENT
    elif direction == "down" and sentiment["has_strongly_positive_headline"]:
        reasons.append(
            "A strongly positive, high-confidence headline vetoes this PE setup "
            "(sentiment is a filter, not a standalone trigger -- but it CAN block)."
        )
        stage = stage or TradingSignal.RejectionStage.SENTIMENT

    # entry_price/stop_loss here are the INDEX's own price/ATR-stop --
    # used only for direction-detection bookkeeping and the NO_TRADE
    # branches' informational entry_price/stop_loss below. The real,
    # executable trade (once a contract resolves further down) is priced
    # in that contract's own premium via option_entry_price/
    # option_stop_loss instead -- see module docstring.
    entry_price = Decimal(str(ind["close"]))
    raw_atr_distance = ind["atr"] * ATR_STOP_MULTIPLIER
    # NaN-safe, same reasoning as the backtest simulator's identical-
    # shape guards above: a gap in ingested candles (plausible on a day
    # with real Angel One rate-limit trouble -- see apps.market_data.
    # broker_client's circuit breaker) can leave ind["atr"] as NaN for
    # the latest bar. Decimal(str(nan)) is a valid Python object but not
    # something MySQL accepts for a DecimalField -- letting it through
    # would crash this signal's DB write instead of logging a clean
    # NO_TRADE, the one thing this function must never do.
    if not (raw_atr_distance > 0):
        reasons.append(
            f"ATR is invalid ({ind['atr']!r}) -- cannot size a stop-loss right now "
            f"(likely a gap in ingested candles)."
        )
        stage = stage or TradingSignal.RejectionStage.RISK
        stop_loss = entry_price
    elif direction == "up":
        stop_loss = entry_price - Decimal(str(raw_atr_distance))
    else:
        stop_loss = entry_price + Decimal(str(raw_atr_distance))

    options_score = 0.0
    options_detail = ""
    strike_detail = ""
    suggested_strike_price = None
    resolved_contract = None
    option_entry_price = None
    option_stop_loss = None
    option_target_1 = None
    option_target_2 = None
    if not reasons:
        options_result = options_confluence_score(underlying, direction=option_direction)
        options_score = options_result["score"]
        options_detail = options_result["detail"]
        if options_result["veto"]:
            reasons.append(f"Options-chain veto: {options_result['veto_reason']}")
            stage = stage or TradingSignal.RejectionStage.OPTIONS_CONFLUENCE
        elif options_score < OPTIONS_SCORE_THRESHOLD:
            reasons.append(
                f"Options-chain confirmation score {options_score:.2f} below "
                f"{OPTIONS_SCORE_THRESHOLD:.2f} threshold. {options_detail}"
            )
            stage = stage or TradingSignal.RejectionStage.OPTIONS_CONFLUENCE

    if not reasons:
        expiry = select_expiry(underlying)
        if expiry is None:
            strike_detail = f"No options contracts synced for {underlying}'s configured expiry mode yet."
            reasons.append(strike_detail)
            stage = stage or TradingSignal.RejectionStage.EXPIRY
        else:
            quality_report = validate_option_chain_snapshot(underlying, expiry)
            if not quality_report.valid:
                reasons.append(f"OPTION DATA UNAVAILABLE: {'; '.join(quality_report.issues)}")
                stage = stage or TradingSignal.RejectionStage.DATA_QUALITY

        if expiry is not None and not reasons:
            suggestion = suggest_best_strike(underlying, expiry, direction=option_direction)
            if suggestion["suggested"] is None:
                reasons.append(f"No suitable {side} strike to suggest: {suggestion['reason']}")
                stage = stage or TradingSignal.RejectionStage.STRIKE
            else:
                strike_detail = f"Suggested {side}: {suggestion['reason']}"
                best = suggestion["suggested"]
                suggested_strike_price = best["strike"]

                # Resolve the actual OptionContract row and price/size the
                # trade in ITS OWN PREMIUM, not the index -- see module
                # docstring. premium_distance scales the same ATR-based
                # index stop-distance every other strategy already uses by
                # this contract's own delta (its own rough "how much
                # premium moves per index point" proxy, already computed
                # by suggest_best_strike), rather than inventing a second,
                # unrelated stop-sizing rule just for this path.
                resolved_contract = OptionContract.objects.get(pk=best["contract_id"])

                liquidity_ok, liquidity_reason = check_option_contract_liquidity(
                    resolved_contract, best.get("bid"), best.get("ask"),
                    best["open_interest"], best["volume"],
                )
                if not liquidity_ok:
                    reasons.append(liquidity_reason)
                    stage = stage or TradingSignal.RejectionStage.LIQUIDITY

                option_entry_price = Decimal(str(best["ltp"]))
                premium_distance = Decimal(str(raw_atr_distance * abs(best["delta"])))
                # Buying a CE or a PE is always a LONG bet on that option's
                # own premium -- unlike the underlying-proxy stop/target
                # above, this shape does NOT flip between up/CE and
                # down/PE.
                option_stop_loss = option_entry_price - premium_distance
                option_target_1 = option_entry_price + premium_distance
                option_target_2 = option_entry_price + premium_distance * 2

                if option_stop_loss <= 0:
                    reasons.append(
                        f"Computed option stop-loss ({option_stop_loss}) would be at/below zero "
                        f"for the {suggested_strike_price} {side} at this premium/ATR -- cannot "
                        f"size a real stop."
                    )
                    stage = stage or TradingSignal.RejectionStage.STRIKE

    if not reasons:
        risk_decision = check_pre_trade(underlying, option_entry_price, option_stop_loss)
        if not risk_decision.approved:
            reasons.extend(risk_decision.reasons)
            stage = stage or TradingSignal.RejectionStage.RISK
        risk_score = risk_decision.risk_score
    else:
        risk_score = 0.0

    if reasons:
        # max(..., 0): consistent with the two other total_score
        # computations below -- sentiment is a veto-only filter, so a
        # merely-negative (but non-vetoing) sentiment_score must not
        # drag total_score down via this average any more here than it
        # does for a setup rejected further down the pipeline.
        total_score = round(
            (technical_score + max(sentiment["sentiment_score"], 0) + risk_score + options_score) / 4, 4,
        )
        return _create_signal(
            symbol=underlying, signal_type=SignalType.NO_TRADE,
            entry_price=entry_price, stop_loss=stop_loss,
            total_score=total_score, technical_score=round(technical_score, 4),
            sentiment_score=sentiment["sentiment_score"], risk_score=risk_score,
            options_score=options_score,
            regime=regime, status=SignalStatus.REJECTED,
            rejection_stage=stage,
            reason=f"[{side} side] " + " ".join(reasons),
            **_indicator_signal_fields(ind),
        )

    # Everything passed: score the ML model BEFORE finalizing
    # position_size (same reason/pattern as generate_signal()) so its
    # win-probability can nudge sizing and, below MIN_WIN_PROBABILITY_
    # TO_TRADE, veto the trade even after every rule-based gate passed.
    from apps.learning.ml_predict import (
        MIN_WIN_PROBABILITY_TO_TRADE,
        ml_confidence_size_multiplier,
        predict_win_probability,
        should_reject_for_low_confidence,
    )

    provisional = SimpleNamespace(
        technical_score=technical_score, sentiment_score=sentiment["sentiment_score"],
        risk_score=risk_score, options_score=options_score, regime=regime, symbol=underlying,
        **_indicator_signal_fields(ind),
    )
    ml_probability = predict_win_probability(provisional)

    if should_reject_for_low_confidence(ml_probability):
        total_score = round(
            (technical_score + max(sentiment["sentiment_score"], 0) + risk_score + options_score) / 4, 4,
        )
        return _create_signal(
            _ml_probability=ml_probability,
            symbol=underlying, signal_type=SignalType.NO_TRADE,
            entry_price=entry_price, stop_loss=stop_loss,
            total_score=total_score, technical_score=round(technical_score, 4),
            sentiment_score=sentiment["sentiment_score"], risk_score=risk_score,
            options_score=options_score,
            regime=regime, status=SignalStatus.REJECTED,
            rejection_stage=TradingSignal.RejectionStage.ML_CONFIDENCE,
            reason=(
                f"[{side} side] Rule-based checks (incl. {side}-side success rate) passed but ML "
                f"win-probability model rates this setup {ml_probability:.0%}, below the "
                f"{MIN_WIN_PROBABILITY_TO_TRADE:.0%} learning-loop threshold -- similar past "
                f"setups mostly lost."
            ),
            **_indicator_signal_fields(ind),
        )

    confidence_multiplier = ml_confidence_size_multiplier(ml_probability)
    size_multiplier = regime_size_multiplier(regime) * confidence_multiplier
    scaled_qty = int(risk_decision.position_size * size_multiplier)

    total_score = round(
        (technical_score + max(sentiment["sentiment_score"], 0) + risk_score + options_score) / 4, 4,
    )

    # Real orders must be a whole multiple of the contract's lot size --
    # rounded down here (not earlier, against risk_decision.position_size
    # before regime/ML scaling) since the regime and ML-confidence
    # multipliers above can still push an already-lot-sized qty below one
    # lot, and this is the true final quantity that would be ordered.
    lots = scaled_qty // resolved_contract.lot_size if resolved_contract.lot_size else 0
    if lots < 1:
        return _create_signal(
            _ml_probability=ml_probability,
            symbol=underlying, signal_type=SignalType.NO_TRADE,
            entry_price=option_entry_price, stop_loss=option_stop_loss,
            total_score=total_score, technical_score=round(technical_score, 4),
            sentiment_score=sentiment["sentiment_score"], risk_score=risk_score,
            options_score=options_score,
            option_side=side, strike_price=suggested_strike_price,
            regime=regime, status=SignalStatus.REJECTED,
            rejection_stage=TradingSignal.RejectionStage.LOT_SIZE,
            reason=(
                f"[{side} side] Rule-based and ML checks passed, but the sized quantity "
                f"({scaled_qty}) rounds down to under one lot (lot_size="
                f"{resolved_contract.lot_size}) for the {suggested_strike_price} {side} "
                f"at {option_entry_price} premium -- no real order can be placed."
            ),
            **_indicator_signal_fields(ind),
        )
    position_size = lots * resolved_contract.lot_size

    ml_note = (
        f" ML win-probability {ml_probability:.0%} (size x{confidence_multiplier})."
        if ml_probability is not None else ""
    )

    return _create_signal(
        _ml_probability=ml_probability,
        symbol=underlying,
        signal_type=SignalType.BUY,
        entry_price=option_entry_price, stop_loss=option_stop_loss,
        target_1=option_target_1, target_2=option_target_2, position_size=position_size,
        option_side=side, strike_price=suggested_strike_price, option_contract=resolved_contract,
        total_score=total_score, technical_score=round(technical_score, 4),
        sentiment_score=sentiment["sentiment_score"], risk_score=risk_score,
        options_score=options_score,
        regime=regime, status=SignalStatus.APPROVED,
        rejection_stage=None,
        reason=(
            f"[{side} side] {direction_result['detail']} {success['detail']} "
            f"Sentiment {sentiment['sentiment_score']:+.2f} over {sentiment['headline_count']} headlines. "
            f"Options-chain: {options_detail} {strike_detail} "
            f"Regime={regime}; risk-approved at {lots} lot(s) ({position_size} qty).{ml_note} "
            f"Executes as a real BUY order on {resolved_contract.tradingsymbol} "
            f"({suggested_strike_price} {side}) at premium {option_entry_price}, in whichever "
            f"execution mode (paper/live) is currently active."
        ),
        **_indicator_signal_fields(ind),
    )
