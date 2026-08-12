"""
The composite scoring engine (manual section 5B: "Composite scoring
engine"). generate_signal() is the one function that ties together:

  apps.market_data.indicators  -> technical conditions + technical_score
  apps.market_data.regime      -> trending / sideways / high_volatility
  apps.news.scoring            -> sentiment_score + contradictory-headline veto
  apps.risk.engine              -> position sizing + hard-limit approve/reject

...and writes exactly one TradingSignal row per call, whatever the
outcome (BUY / NO_TRADE, approved / rejected) -- per the manual's
"every decision must be logged" principle, this function has no
"return early without saving anything" path once it has committed to
evaluating a symbol.
"""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from apps.market_data.indicators import compute_indicators
from apps.market_data.regime import classify_regime, regime_size_multiplier
from apps.news.scoring import aggregate_sentiment
from apps.options.signals_engine import options_confluence_score
from apps.risk.engine import check_pre_trade
from common.constants import MarketRegime, SignalStatus, SignalType

from .models import TradingSignal

# ATR multiplier for the stop-loss distance -- manual section 13,
# "Stop-loss rules: ATR multiplier 1.2x to 1.8x". 1.5 sits in the
# middle of that range as a starting point; tightening/loosening this
# is exactly the kind of "soft parameter" the daily learning loop
# (manual section 15) is allowed to tune later -- it is NOT one of the
# settings.RISK_HARD_LIMITS values.
ATR_STOP_MULTIPLIER = 1.5

# How many of the technical buy conditions must be true before a setup
# even reaches the sentiment/options/risk stage. Not 100% -- "multi-
# confirmation logic is safer than a single-indicator trigger" (section
# 11) means requiring most, not all, conditions (real markets rarely
# line up every single indicator at once; requiring 100% would mean
# almost no trades ever fire, which cuts against "fewer high-quality
# trades" by making the bar so high it stops being a *quality* filter
# and becomes a *no-trade* filter).
#
# Raised from 0.7 to 0.8 on 2026-08-08 based on apps.analytics.backtest's
# walk_forward_backtest against real NIFTY/BANKNIFTY history (~25k real
# candles backfilled from Angel One that day): 0.8 was the selected
# (train-slice-best) threshold for 5 of 6 symbol/timeframe combos tested,
# and held up on the untouched test slice too (e.g. BANKNIFTY 15m:
# +0.124R train vs +0.125R test -- as consistent as a backtest gets).
# Revisit if/when there's enough real paper-trade history for
# apps.learning's own daily review to have an independent opinion.
TECHNICAL_SCORE_THRESHOLD = 0.8

# Minimum options-chain confirmation score (see
# apps.options.signals_engine.options_confluence_score) required for a
# technically-strong setup to still go through -- options flow
# disagreeing with the technical direction (e.g. EMA/MACD bullish but
# heavy put buying / bearish put unwinding) is exactly the kind of
# cross-signal disagreement the manual's multi-confirmation principle
# exists to catch, same role sentiment's contradictory-headline veto
# plays for news.
OPTIONS_SCORE_THRESHOLD = 0.35

# manual section 12: high-volatility regime gets "stricter filters AND
# smaller size" -- both halves, not just size. Added to
# TECHNICAL_SCORE_THRESHOLD for that regime only (with 8 boolean
# conditions, +0.125 raises the effective bar by one full condition,
# e.g. 7/8 -> 8/8). Previously this regime unconditionally rejected
# every setup regardless of score, which made regime_size_multiplier's
# documented 0.5x high-volatility sizing dead code -- fixed 2026-08-08
# so high-volatility setups can still trade, just held to a higher bar
# and sized down, matching what regime_size_multiplier already claimed.
HIGH_VOLATILITY_SCORE_MARGIN = 0.125


def _indicator_signal_fields(ind: dict | None) -> dict:
    """
    Builds the ind_* kwargs for _create_signal() from the compute_indicators()
    dict, normalizing the price-scale values (atr, macd_hist, ema slopes) by
    the same candle's close so they're comparable across symbols at very
    different price levels -- see TradingSignal's ind_* field docstrings.
    Returns an all-None dict (not a KeyError) when ind is None, matching the
    no-historical-data NO_TRADE branch below, which never reaches a real
    indicator snapshot.
    """
    if ind is None:
        return {
            "ind_rsi": None, "ind_adx": None, "ind_bb_width": None,
            "ind_relative_volume": None, "ind_atr_pct": None,
            "ind_macd_hist_pct": None, "ind_ema9_slope_pct": None,
            "ind_ema21_slope_pct": None,
        }
    close = ind["close"] or 1.0  # guard divide-by-zero; close is never legitimately 0
    return {
        "ind_rsi": ind["rsi"],
        "ind_adx": ind["adx"],
        "ind_bb_width": ind["bb_width"],
        "ind_relative_volume": ind["relative_volume"],
        "ind_atr_pct": ind["atr"] / close,
        "ind_macd_hist_pct": ind["macd_hist"] / close,
        "ind_ema9_slope_pct": ind["ema9_slope"] / close,
        "ind_ema21_slope_pct": ind["ema21_slope"] / close,
    }


def _create_signal(_ml_probability: float | None = "unset", **kwargs) -> TradingSignal:
    """
    The only place generate_signal() actually calls
    TradingSignal.objects.create() -- every new signal (whichever
    branch below produced it: no-data, rejected, or approved) is routed
    through here so it also gets scored by the win-probability ML model
    (apps.learning.ml_predict), the same way, every time.

    _ml_probability: pass an already-computed probability (the approved
    branch needs it BEFORE creating the row, to size the position -- see
    ml_confidence_size_multiplier's call site below) to avoid scoring
    the same signal twice. Leave at the "unset" sentinel to have this
    function predict it fresh, which is what the no-data/rejected
    branches do.

    This does NOT feed back into technical_score/total_score or the
    approved/rejected decision above -- ml_win_probability is stored as
    an extra, purely advisory column for the dashboard/daily review to
    read alongside the four rule-based scores (see that field's
    docstring in apps/signals/models.py). Lazy import, same pattern as
    apps.market_data.broker_client and apps.news.finbert -- importing
    apps.signals.engine must not force scikit-learn/joblib to load in
    processes that never generate a signal.
    """
    signal = TradingSignal.objects.create(**kwargs)

    from apps.learning.ml_predict import predict_win_probability

    probability = predict_win_probability(signal) if _ml_probability == "unset" else _ml_probability
    if probability is not None:
        signal.ml_win_probability = probability
        signal.save(update_fields=["ml_win_probability"])
    return signal


def _evaluate_buy_conditions(ind: dict) -> dict[str, bool]:
    """
    Every condition from manual section 11's "Buy conditions" list that
    is computable from price/volume alone (sentiment and regime are
    evaluated separately, not folded in here, so this dict's true/false
    count can become a clean technical_score on its own).
    """
    return {
        "price_above_ema9_ema21": ind["close"] > ind["ema9"] > ind["ema21"],
        "ema9_slope_positive": ind["ema9_slope"] > 0,
        "ema21_slope_flat_or_positive": ind["ema21_slope"] >= 0,
        "sar_below_price": ind["sar"] < ind["close"],
        "macd_above_signal": ind["macd"] > ind["macd_signal"],
        "histogram_rising": ind["macd_hist"] > ind["macd_hist_prev"],
        "rsi_above_regime_threshold": ind["rsi"] > 50,  # regime-adjusted below in generate_signal
        "relative_volume_breakout": ind["relative_volume"] >= 1.2,
    }


def _evaluate_exit_conditions(ind: dict) -> dict[str, bool]:
    """
    manual section 11 "Sell / exit conditions" -- used by
    should_exit_position() for OPEN positions, not for new-entry
    scoring. Time-stop and stop-loss/trailing-stop triggers aren't
    included here since those depend on the specific position's entry
    time and stop price, not just current indicators; apps.execution
    (not yet written) is where those get checked against a live
    OpenPosition row.
    """
    return {
        "close_below_ema9_2_candles": ind["close_below_ema9_streak"] >= 2,
        "sar_flipped_above_price": ind["sar"] > ind["close"],
        "macd_histogram_weakening": ind["macd_hist"] < ind["macd_hist_prev"],
        "rsi_losing_momentum": ind["rsi"] < 50,
        "volume_fading": ind["relative_volume"] < 0.8,
    }


def should_exit_position(symbol: str, timeframe: str = "5m") -> tuple[bool, list[str]]:
    """
    Convenience function for apps.execution to call once it exists.
    Returns (should_exit, matched_reasons) using a simple majority rule
    over _evaluate_exit_conditions, mirroring the multi-confirmation
    approach used for entries.
    """
    ind = compute_indicators(symbol, timeframe)
    if ind is None:
        return False, ["Not enough historical data to evaluate exit conditions."]

    conditions = _evaluate_exit_conditions(ind)
    matched = [name for name, ok in conditions.items() if ok]
    return (len(matched) / len(conditions)) >= 0.5, matched


def generate_signal(symbol: str, timeframe: str = "5m") -> TradingSignal:
    """
    Always returns a saved TradingSignal, never None -- if anything
    short-circuits the evaluation (no data, technical setup too weak,
    contradictory headline, risk engine rejection), that outcome is
    itself the signal being logged, with `status` and `reason`
    explaining why, not an early return that skips logging entirely.
    """
    ind = compute_indicators(symbol, timeframe)

    if ind is None:
        return _create_signal(
            symbol=symbol, signal_type=SignalType.NO_TRADE,
            entry_price=0, stop_loss=0,
            total_score=0, technical_score=0, sentiment_score=0, risk_score=0, options_score=0,
            regime=MarketRegime.SIDEWAYS,
            status=SignalStatus.REJECTED,
            reason="Not enough historical candles yet to compute indicators.",
            **_indicator_signal_fields(None),
        )

    regime = classify_regime(ind)
    conditions = _evaluate_buy_conditions(ind)
    # Regime-adjusted RSI threshold (section 11: "RSI above
    # regime-adjusted threshold") -- sideways markets need a higher bar
    # since momentum breakouts are less reliable there.
    if regime == MarketRegime.SIDEWAYS:
        conditions["rsi_above_regime_threshold"] = ind["rsi"] > 60

    matched = [name for name, ok in conditions.items() if ok]
    technical_score = len(matched) / len(conditions)

    sentiment = aggregate_sentiment(symbol)
    entry_price = Decimal(str(ind["close"]))
    stop_loss = entry_price - Decimal(str(ind["atr"] * ATR_STOP_MULTIPLIER))

    reasons: list[str] = []

    # High-volatility regime holds setups to a higher technical bar
    # (manual section 12's "stricter filters" half) on top of the
    # smaller size applied later via regime_size_multiplier() -- it no
    # longer blocks every high-volatility setup outright.
    effective_score_threshold = TECHNICAL_SCORE_THRESHOLD
    if regime == MarketRegime.HIGH_VOLATILITY:
        effective_score_threshold = min(1.0, TECHNICAL_SCORE_THRESHOLD + HIGH_VOLATILITY_SCORE_MARGIN)

    if technical_score < effective_score_threshold:
        unmatched = [name for name, ok in conditions.items() if not ok]
        threshold_note = (
            " (raised for the high-volatility regime -- stricter filters apply, "
            "manual section 12)" if regime == MarketRegime.HIGH_VOLATILITY else ""
        )
        reasons.append(
            f"Technical score {technical_score:.0%} below the {effective_score_threshold:.0%} "
            f"threshold{threshold_note}. Unmet conditions: {', '.join(unmatched)}."
        )

    if sentiment["has_contradictory_headline"]:
        reasons.append(
            "A strongly negative, high-confidence headline vetoes this setup "
            "(sentiment is a filter, not a standalone trigger -- but it CAN block)."
        )

    # Only consult options-chain confirmation (and, below, the risk
    # engine) if the technical+sentiment case is actually worth
    # evaluating further -- same "don't spam checks on setups that were
    # never going anywhere" reasoning that already applied to the risk
    # engine below, now extended to the options-flow check too, since
    # it hits the DB (OptionContract/OptionChainSnapshot) same as the
    # risk engine does.
    options_score = 0.0
    options_detail = ""
    if not reasons:
        options_result = options_confluence_score(symbol, direction="bullish")
        options_score = options_result["score"]
        options_detail = options_result["detail"]
        if options_result["veto"]:
            reasons.append(f"Options-chain veto: {options_result['veto_reason']}")
        elif options_score < OPTIONS_SCORE_THRESHOLD:
            reasons.append(
                f"Options-chain confirmation score {options_score:.2f} below "
                f"{OPTIONS_SCORE_THRESHOLD:.2f} threshold. {options_detail}"
            )

    # Only consult the risk engine if the technical+sentiment+options
    # case is actually worth risking capital on. Calling
    # check_pre_trade() for every rejected setup would spam
    # apps.risk.RiskEvent with noise for setups that were never going
    # anywhere -- the risk log should reflect genuine risk decisions,
    # not every failed upstream screen.
    if not reasons:
        risk_decision = check_pre_trade(symbol, entry_price, stop_loss)
        if not risk_decision.approved:
            reasons.extend(risk_decision.reasons)
            risk_score = risk_decision.risk_score
        else:
            risk_score = risk_decision.risk_score
    else:
        risk_score = 0.0

    if reasons:
        total_score = round(
            (technical_score + sentiment["sentiment_score"] + risk_score + options_score) / 4, 4,
        )
        return _create_signal(
            symbol=symbol, signal_type=SignalType.NO_TRADE,
            entry_price=entry_price, stop_loss=stop_loss,
            total_score=total_score, technical_score=round(technical_score, 4),
            sentiment_score=sentiment["sentiment_score"], risk_score=risk_score,
            options_score=options_score,
            regime=regime, status=SignalStatus.REJECTED,
            reason=" ".join(reasons),
            **_indicator_signal_fields(ind),
        )

    # Everything passed: technical setup strong, no contradictory
    # sentiment, options-chain flow not disagreeing, regime acceptable,
    # and risk engine approved with a concrete position size.
    #
    # Score the ML model BEFORE finalizing position_size (not after,
    # via _create_signal's own fallback) so its win-probability can
    # nudge sizing within the narrow, bounded band
    # ml_confidence_size_multiplier defines -- a SimpleNamespace stands
    # in for the not-yet-created TradingSignal row since
    # vector_for_signal only reads plain attributes off it.
    from types import SimpleNamespace

    from apps.learning.ml_predict import (
        MIN_WIN_PROBABILITY_TO_TRADE,
        ml_confidence_size_multiplier,
        predict_win_probability,
        should_reject_for_low_confidence,
    )

    provisional = SimpleNamespace(
        technical_score=technical_score, sentiment_score=sentiment["sentiment_score"],
        risk_score=risk_score, options_score=options_score, regime=regime, symbol=symbol,
        **_indicator_signal_fields(ind),
    )
    ml_probability = predict_win_probability(provisional)

    # The learning-loop veto: only fires once a model actually exists
    # (ml_probability is not None) AND it scores this setup below
    # MIN_WIN_PROBABILITY_TO_TRADE, i.e. "historically, setups shaped
    # like this one mostly lost." Checked here -- after every rule-based
    # gate (technical/sentiment/options/risk) already passed -- so it is
    # a final, additive filter on top of the existing logic, never a
    # replacement for it.
    if should_reject_for_low_confidence(ml_probability):
        total_score = round(
            (technical_score + max(sentiment["sentiment_score"], 0) + risk_score + options_score) / 4, 4,
        )
        return _create_signal(
            _ml_probability=ml_probability,
            symbol=symbol, signal_type=SignalType.NO_TRADE,
            entry_price=entry_price, stop_loss=stop_loss,
            total_score=total_score, technical_score=round(technical_score, 4),
            sentiment_score=sentiment["sentiment_score"], risk_score=risk_score,
            options_score=options_score,
            regime=regime, status=SignalStatus.REJECTED,
            reason=(
                f"Rule-based checks passed but ML win-probability model rates this "
                f"setup {ml_probability:.0%}, below the {MIN_WIN_PROBABILITY_TO_TRADE:.0%} "
                f"learning-loop threshold -- similar past setups mostly lost."
            ),
            **_indicator_signal_fields(ind),
        )

    confidence_multiplier = ml_confidence_size_multiplier(ml_probability)

    size_multiplier = regime_size_multiplier(regime) * confidence_multiplier
    position_size = int(risk_decision.position_size * size_multiplier)
    target_1 = entry_price + (entry_price - stop_loss)       # 1R
    target_2 = entry_price + (entry_price - stop_loss) * 2   # 2R

    total_score = round(
        (technical_score + max(sentiment["sentiment_score"], 0) + risk_score + options_score) / 4, 4,
    )

    ml_note = (
        f" ML win-probability {ml_probability:.0%} (size x{confidence_multiplier})."
        if ml_probability is not None else ""
    )

    return _create_signal(
        _ml_probability=ml_probability,
        symbol=symbol, signal_type=SignalType.BUY,
        entry_price=entry_price, stop_loss=stop_loss,
        target_1=target_1, target_2=target_2, position_size=position_size,
        total_score=total_score, technical_score=round(technical_score, 4),
        sentiment_score=sentiment["sentiment_score"], risk_score=risk_score,
        options_score=options_score,
        regime=regime, status=SignalStatus.APPROVED,
        reason=(
            f"Matched {len(matched)}/{len(conditions)} technical conditions "
            f"({', '.join(matched)}); sentiment {sentiment['sentiment_score']:+.2f} "
            f"over {sentiment['headline_count']} headlines; options-chain: {options_detail} "
            f"regime={regime}; risk-approved at size {position_size}.{ml_note}"
        ),
        **_indicator_signal_fields(ind),
    )
