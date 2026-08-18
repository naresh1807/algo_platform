"""
Final Signal Assembly: composes an already-created apps.signals.models.
TradingSignal (+ its resolved OptionContract, when one exists) together
with every read-only analytics engine built across this platform's
Options Intelligence phases (confirmation, scoring, explanation,
strategy classification, expected move, support/resistance) into the
single structured object a caller (the REST API, the frontend terminal)
can render directly -- the platform's answer to "never say BUY NIFTY
as the final executable signal": every field below is either an EXACT
resolved contract identifier or an explicit None, never a fabricated
placeholder.

This module is a READ-ONLY VIEW BUILDER, not a second trading-decision
pipeline -- it never re-runs risk checks or re-decides approve/reject;
it reads what apps.options.index_direction_strategy already decided
and decorates it with analytics for display.
"""

from __future__ import annotations

from common.constants import SignalStatus


def _status_label(signal) -> str:
    if signal.status in (SignalStatus.APPROVED, SignalStatus.EXECUTED):
        return "SIGNAL_READY"
    if signal.rejection_stage == "data_quality":
        return "DATA_INVALID"
    if signal.rejection_stage in ("liquidity", "risk"):
        return "TRADE_BLOCKED"
    return "NO_TRADE"


def _risk_reward(signal) -> float | None:
    if signal.entry_price is None or signal.stop_loss is None or signal.target_1 is None:
        return None
    risk = abs(float(signal.entry_price) - float(signal.stop_loss))
    reward = abs(float(signal.target_1) - float(signal.entry_price))
    if risk == 0:
        return None
    return round(reward / risk, 4)


def resolve_final_signal(signal) -> dict:
    """
    signal: an apps.signals.models.TradingSignal instance (typically
    the latest one for an underlying, from apps.options.
    index_direction_strategy.evaluate_index_direction_trade).

    Returns the structured object matching this platform's Section-43-
    style final signal shape. Every field degrades to None (never a
    guess) when its inputs aren't available -- e.g. a REJECTED signal
    with no resolved option_contract has expiry/strike/optionType/
    tradingSymbol/instrumentToken/greeks all None, and "decision" is
    "NO_TRADE", exactly reflecting that no exact contract was ever
    resolved for it.
    """
    contract = signal.option_contract
    direction = "bullish" if signal.option_side == "CE" else "bearish" if signal.option_side == "PE" else None

    greeks = {"delta": None, "gamma": None, "theta": None, "vega": None}
    option_ltp = None
    support: list[dict] = []
    resistance: list[dict] = []
    expected_move: dict = {"expected_move": None, "upper_range": None, "lower_range": None}
    confirmation_result: dict | None = None
    scoring_result: dict | None = None
    explanation = {"positive_factors": [], "negative_factors": [], "risk_flags": []}
    strategy_result = {"strategy": "NO_TRADE", "executable": True, "reason": "No option contract resolved for this signal."}
    latest_snapshot = None

    if contract is not None:
        from .greeks import compute_greeks_for_contract
        from .metrics import _latest_snapshots
        from .pricing import latest_ltp_for_contract
        from .signals_engine import _latest_underlying_ltp

        spot = _latest_underlying_ltp(contract.underlying, contract.expiry)
        latest_snapshot = next(
            (s for s in _latest_snapshots(contract.underlying, contract.expiry) if s.contract_id == contract.pk),
            None,
        )
        option_ltp = float(latest_snapshot.ltp) if latest_snapshot and latest_snapshot.ltp is not None else (
            latest_ltp_for_contract(contract)
        )

        if spot is not None and option_ltp is not None:
            computed_greeks = compute_greeks_for_contract(contract, spot, option_ltp)
            if computed_greeks is not None:
                greeks = {k: computed_greeks[k] for k in ("delta", "gamma", "theta", "vega")}

        if spot is not None:
            from .expected_move import calculate_expected_move_for_contract
            from .support_resistance import calculate_dynamic_resistance, calculate_dynamic_support

            expected_move = calculate_expected_move_for_contract(contract.underlying, contract.expiry, spot)
            support = calculate_dynamic_support(contract.underlying, contract.expiry, spot)
            resistance = calculate_dynamic_resistance(contract.underlying, contract.expiry, spot)

            if direction is not None:
                from .confirmation import evaluate_multi_signal_confirmation
                from .signal_scoring import calculate_signal_score

                # apps.options.confirmation's factor evaluators expect
                # RAW slope/macd_hist values and divide by ind["close"]
                # themselves (matching apps.signals.engine's own
                # _indicator_signal_fields convention) -- TradingSignal
                # only stores the already-normalized *_pct fields, so
                # multiplying back by `spot` here exactly reconstructs
                # the raw value (the multiply/divide cancel exactly,
                # not an approximation), letting this reuse the same
                # evaluators rather than a second normalization scheme.
                ind = {
                    "close": spot, "ema9_slope": signal.ind_ema9_slope_pct * spot if signal.ind_ema9_slope_pct is not None else None,
                    "ema21_slope": signal.ind_ema21_slope_pct * spot if signal.ind_ema21_slope_pct is not None else None,
                    "macd_hist": signal.ind_macd_hist_pct * spot if signal.ind_macd_hist_pct is not None else None,
                    "relative_volume": signal.ind_relative_volume,
                }
                confirmation_result = evaluate_multi_signal_confirmation(
                    contract.underlying, contract.expiry, direction, ind, spot,
                    delta=greeks["delta"], liquidity_score=None, risk_reward=_risk_reward(signal),
                )
                scoring_result = calculate_signal_score(confirmation_result)
                explanation = build_explanation(confirmation_result)

                from .metrics import compute_iv_rank
                iv_rank = None
                if latest_snapshot is not None and latest_snapshot.iv is not None:
                    history = [
                        s.iv for s in _latest_snapshots(contract.underlying, contract.expiry)
                        if s.contract_id == contract.pk and s.iv is not None
                    ]
                    iv_rank = compute_iv_rank(latest_snapshot.iv, history) if history else None

                from .expected_move import classify_price_vs_expected_range

                em_position = classify_price_vs_expected_range(spot, expected_move.get("upper_range"), expected_move.get("lower_range"))
                strategy_result = classify_strategy_for_signal(direction, signal.regime, iv_rank, em_position, confirmation_result.get("conflict_level"))

    return {
        "status": _status_label(signal),
        "underlying": signal.symbol,
        "marketRegime": signal.regime,
        "direction": direction,

        "expiry": contract.expiry.isoformat() if contract else None,
        "strike": float(contract.strike) if contract else None,
        "optionType": contract.option_type if contract else None,
        "tradingSymbol": contract.tradingsymbol if contract else None,
        "instrumentToken": contract.symbol_token if contract else None,

        "optionLTP": option_ltp,
        "delta": greeks["delta"], "gamma": greeks["gamma"], "theta": greeks["theta"], "vega": greeks["vega"],
        # IV isn't a field on TradingSignal itself -- read from the
        # contract's own latest snapshot (already fetched above) rather
        # than guessed or left permanently absent.
        "iv": latest_snapshot.iv if latest_snapshot is not None else None,

        "signalScore": scoring_result["total_score"] if scoring_result else signal.total_score,
        "confidence": signal.ml_win_probability,
        "estimatedProbability": signal.ml_win_probability,

        "entry": float(signal.entry_price) if signal.entry_price is not None else None,
        "stopLoss": float(signal.stop_loss) if signal.stop_loss is not None else None,
        "targets": [float(t) for t in (signal.target_1, signal.target_2) if t is not None],

        "riskReward": _risk_reward(signal),
        "positionSize": float(signal.position_size) if signal.position_size is not None else None,

        "support": support, "resistance": resistance, "expectedMove": expected_move,

        "positiveFactors": explanation["positive_factors"],
        "negativeFactors": explanation["negative_factors"],
        "riskFlags": explanation["risk_flags"],

        "strategy": strategy_result["strategy"],
        "reasoning": signal.reason,

        "decision": signal.signal_type.upper() if signal.signal_type else "NO_TRADE",
    }


def build_explanation(confirmation_result: dict) -> dict:
    from .signal_explanation import build_signal_explanation

    return build_signal_explanation(confirmation_result)


def classify_strategy_for_signal(direction, regime, iv_rank, em_position, conflict_level) -> dict:
    from .strategy_selector import classify_strategy

    return classify_strategy(direction, regime, iv_rank, em_position, conflict_level)
