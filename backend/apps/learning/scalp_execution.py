"""
Real (paper-mode) option-position execution for the SCALPING comparison
methods (apps.learning.strategy_methods.SCALPING_METHOD_FUNCS) -- an
ADDITIONAL path alongside, not a replacement for,
apps.learning.tasks._run_comparison_cycle's own isolated HypotheticalTrade
comparison (which stays completely untouched: same 1-minute cadence, same
index-priced simulation, same scalp_win_probability model feeding off it).

Deliberately its own module, not folded into strategy_methods.py (that
module's own docstring: "PURE and READ-ONLY... No DB writes happen here")
or into _run_comparison_cycle (which must keep behaving identically for
the method-comparison ranking to stay meaningful) -- this is a genuinely
different concern: taking one of those same idea-generator functions'
output and actually trading it for real, through the same risk-engine +
option-contract pipeline apps.options.index_direction_strategy already
uses for the NIFTY/BANKNIFTY CE/PE strategy.

CONSEQUENCE, stated up front (same honesty convention as
index_direction_strategy's own docstring): apps.risk.engine._check_exposure
allows only ONE open position per symbol, platform-wide, shared across
every strategy that touches apps.execution.OpenPosition. Once this module
is scheduled, the three scalping methods below and index_direction_strategy
are all competing for the same NIFTY/BANKNIFTY "slot" in real terms --
whichever fires and gets approved first holds it; the others are rejected
with "already open" until it closes. Scalp positions self-close within
their own short holding cap, so this is a natural, bounded throttle, not a
lasting lockout -- but it does mean these strategies can no longer all hold
a position in the same symbol simultaneously the way the isolated
HypotheticalTrade comparison still can. Real wins/losses here also count
toward the platform's real AccountEquity/kill-switch/drawdown/consecutive-
loss counters, same as index_direction_strategy's trades already do.

PAPER-ONLY, unconditionally: apps.execution.live_executor was never
updated to place a real NFO option order (see index_direction_strategy's
own docstring for why) -- routing an option_contract-bearing signal
through it would place a live order on the wrong instrument (the
underlying) if EXECUTION_MODE were ever "live". This module calls
apps.execution.paper_executor directly, never apps.execution.live_executor,
and never reads apps.execution.models.get_execution_mode().
"""

from __future__ import annotations

from decimal import Decimal

from apps.market_data.regime import classify_regime
from apps.signals.engine import _create_signal, _evaluate_buy_conditions, _indicator_signal_fields
from apps.signals.models import TradingSignal
from common.constants import MarketRegime, SignalStatus, SignalType


def evaluate_and_open_scalp(method: str, generate_idea, underlying: str) -> TradingSignal | None:
    """
    Runs one scalping method's idea generator for `underlying`; if it
    fires, resolves a real option contract, prices/sizes the trade in
    that contract's own premium, and -- if every check clears -- opens a
    real paper OpenPosition for it.

    Returns None (nothing logged) if the idea generator itself returns
    None -- matches _run_comparison_cycle's own existing convention of
    not creating a row when no idea fired at all; a TradingSignal is only
    ever created once there's an actual decision worth logging. Returns
    the saved TradingSignal otherwise (NO_TRADE if any subsequent check
    failed, APPROVED -- with a real OpenPosition already opened -- if
    everything cleared).
    """
    idea = generate_idea(underlying, "1m")
    if idea is None:
        return None

    ind = idea.get("ind")
    entry_price = Decimal(str(idea["entry_price"]))
    stop_loss = Decimal(str(idea["stop_loss"]))

    # Real technical_score/regime (not a fabricated "always confident"
    # placeholder) -- this signal's row is what
    # apps.learning.ml_train._training_rows feeds the REAL win_probability
    # model once its position closes (that function reads every closed
    # OpenPosition with a linked signal, regardless of which module
    # created it), so a hardcoded constant here would quietly inject
    # meaningless feature values into that model's training data.
    # _evaluate_buy_conditions is the right read even for the SAR-burst
    # method (whose own entry logic differs) -- all three scalping ideas
    # are long-only, so "how many of the general bullish conditions are
    # also true right now" is still a meaningful, real technical read of
    # the same moment, not a mismatched borrowed number.
    if ind is not None:
        buy_conditions = _evaluate_buy_conditions(ind)
        technical_score = round(sum(buy_conditions.values()) / len(buy_conditions), 4)
        regime = classify_regime(ind)
    else:
        technical_score = 0.0
        regime = MarketRegime.SIDEWAYS

    # sentiment_score/options_score stay 0.0 (neutral, not fabricated-
    # positive) -- unlike index_direction_strategy's 5-minute cycle, this
    # runs every 1 minute x 3 methods x len(WATCHLIST), and neither
    # aggregate_sentiment nor options_confluence_score gates entry here
    # (not part of this path's approved scope); leaving them at an honest
    # "not evaluated" 0.0 avoids implying a confirmation that never
    # happened, in either the trade's own `reason` text or the real
    # win_probability model's training data.
    sentiment_score = 0.0
    options_score = 0.0

    # All three scalping methods are long-only (strategy_methods.py's own
    # module docstring), so direction is always bullish -> CE, same read
    # apps.learning.tasks._suggest_option_for_idea already uses.
    side = "CE"
    reasons: list[str] = []
    resolved_contract = None
    option_entry_price = None
    option_stop_loss = None
    option_target = None
    suggested_strike_price = None
    strike_detail = ""

    from apps.options.models import OptionContract
    from apps.options.signals_engine import nearest_expiry
    from apps.options.strike_selector import suggest_best_strike

    expiry = nearest_expiry(underlying)
    if expiry is None:
        reasons.append(f"No options contracts synced for {underlying} yet.")
    else:
        suggestion = suggest_best_strike(underlying, expiry, direction="bullish")
        if suggestion["suggested"] is None:
            reasons.append(f"No suitable {side} strike to suggest: {suggestion['reason']}")
        else:
            strike_detail = f"Suggested {side}: {suggestion['reason']}"
            best = suggestion["suggested"]
            suggested_strike_price = best["strike"]
            resolved_contract = OptionContract.objects.get(pk=best["contract_id"])

            # Reuses THIS method's own reward:risk shape (0.6x/0.5x ATR
            # stop with a 1.0R/1.2R target, SAR-level stop with a 1.5R
            # target, ...) instead of hardcoding one for all three --
            # index_distance/target_r are derived from the idea's own
            # entry/stop/target, the same delta-scaled-distance recipe
            # index_direction_strategy uses, just sourced from the idea
            # dict instead of a freshly-computed ATR distance.
            index_distance = entry_price - stop_loss
            target_r = (Decimal(str(idea["target_price"])) - entry_price) / index_distance
            premium_distance = Decimal(str(float(index_distance) * abs(best["delta"])))

            option_entry_price = Decimal(str(best["ltp"]))
            option_stop_loss = option_entry_price - premium_distance
            option_target = option_entry_price + premium_distance * target_r

            if option_stop_loss <= 0:
                reasons.append(
                    f"Computed option stop-loss ({option_stop_loss}) would be at/below zero "
                    f"for the {suggested_strike_price} {side} at this premium -- cannot size a real stop."
                )

    if not reasons:
        from apps.risk.engine import check_pre_trade

        risk_decision = check_pre_trade(underlying, option_entry_price, option_stop_loss)
        if not risk_decision.approved:
            reasons.extend(risk_decision.reasons)
            risk_score = risk_decision.risk_score
        else:
            risk_score = risk_decision.risk_score
    else:
        risk_score = 0.0

    ind_fields = _indicator_signal_fields(ind)
    # Same total_score average every other pipeline in this codebase uses
    # (apps.signals.engine.generate_signal/index_direction_strategy) --
    # kept consistent so this field means the same thing regardless of
    # which module produced the signal.
    total_score = round((technical_score + max(sentiment_score, 0) + risk_score + options_score) / 4, 4)

    if reasons:
        return _create_signal(
            symbol=underlying, signal_type=SignalType.NO_TRADE,
            entry_price=option_entry_price or entry_price,
            stop_loss=option_stop_loss or stop_loss,
            option_side=side if resolved_contract else None,
            strike_price=suggested_strike_price,
            total_score=total_score, technical_score=technical_score, sentiment_score=sentiment_score,
            risk_score=risk_score, options_score=options_score, regime=regime, status=SignalStatus.REJECTED,
            reason=f"[{method} real-execution, {side} side] " + " ".join(reasons),
            **ind_fields,
        )

    lots = risk_decision.position_size // resolved_contract.lot_size if resolved_contract.lot_size else 0
    if lots < 1:
        return _create_signal(
            symbol=underlying, signal_type=SignalType.NO_TRADE,
            entry_price=option_entry_price, stop_loss=option_stop_loss,
            option_side=side, strike_price=suggested_strike_price,
            total_score=total_score, technical_score=technical_score, sentiment_score=sentiment_score,
            risk_score=risk_score, options_score=options_score, regime=regime, status=SignalStatus.REJECTED,
            reason=(
                f"[{method} real-execution, {side} side] Sized quantity "
                f"({risk_decision.position_size}) rounds down to under one lot "
                f"(lot_size={resolved_contract.lot_size}) for {suggested_strike_price} {side} "
                f"at {option_entry_price} premium -- no real order can be placed."
            ),
            **ind_fields,
        )
    position_size = lots * resolved_contract.lot_size

    signal = _create_signal(
        symbol=underlying, signal_type=SignalType.BUY,
        entry_price=option_entry_price, stop_loss=option_stop_loss,
        target_1=option_target, position_size=position_size,
        option_side=side, strike_price=suggested_strike_price, option_contract=resolved_contract,
        total_score=total_score, technical_score=technical_score, sentiment_score=sentiment_score,
        risk_score=risk_score, options_score=options_score, regime=regime, status=SignalStatus.APPROVED,
        reason=(
            f"[{method} real-execution, {side} side] {strike_detail} "
            f"risk-approved at {lots} lot(s) ({position_size} qty). "
            f"Executes as a real BUY order on {resolved_contract.tradingsymbol} "
            f"at premium {option_entry_price}, paper mode only "
            f"(see apps.learning.scalp_execution's module docstring)."
        ),
        **ind_fields,
    )

    from apps.execution.paper_executor import open_position_from_signal

    try:
        open_position_from_signal(
            signal,
            timeframe="1m",
            strategy_key=method,
            enforce_execution_risk=True,
        )
    except ValueError:
        # position_size rounded to 0 inside open_position_from_signal --
        # shouldn't happen given the lot check above, but that function
        # already marks the signal REJECTED and logs why on this exact
        # failure mode, so nothing further to do here.
        pass

    return signal
