"""
Feature Store: builds the feature vector apps.signals.models.
SignalFeatureSnapshot stores, reusing whatever confirmation/scoring
engines this platform already has (apps.options.confirmation,
signal_scoring, greeks, metrics, expected_move) rather than a second,
divergent feature-computation scheme -- training/backtesting should
read exactly what a live signal's own scoring pipeline saw.

Wired via apps.signals.signals.py's post_save receiver (best-effort,
same discipline as that file's existing WS-broadcast receiver), NOT a
change to apps.options.index_direction_strategy's own control flow.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_feature_vector(signal) -> dict:
    """
    Returns the feature dict for one TradingSignal. Every field is None
    (never guessed) when its own inputs aren't available -- e.g. a
    signal with no resolved option_contract has delta/theta/vega/gamma/
    strikeDistance/expiry all None, and every *Score field stays None
    too (there is no direction to score factors against).
    """
    from apps.market_data.time_of_day import calculate_time_of_day_regime

    contract = signal.option_contract
    direction = "bullish" if signal.option_side == "CE" else "bearish" if signal.option_side == "PE" else None

    vector = {
        "trendScore": None, "momentumScore": None, "oiScore": None, "volumeScore": None,
        "ivScore": None, "skewScore": None, "liquidityScore": None, "gammaScore": None,
        "expectedMoveScore": None,
        "orderFlowScore": None,  # always None -- apps.options.order_flow is an honest stub, no data source exists
        "riskRewardScore": None,
        "marketRegime": signal.regime,
        "timeOfDay": calculate_time_of_day_regime().get("phase"),
        "expiry": contract.expiry.isoformat() if contract else None,
        "strikeDistance": None,
        "delta": None, "theta": None, "vega": None, "gamma": None,
    }

    if contract is None or direction is None:
        return vector

    from .greeks import compute_greeks_for_contract
    from .metrics import _latest_snapshots, compute_iv_rank
    from .signals_engine import _latest_underlying_ltp

    spot = _latest_underlying_ltp(contract.underlying, contract.expiry)
    if spot is None:
        return vector

    vector["strikeDistance"] = round(abs(float(contract.strike) - spot), 2)

    contract_snapshots = [
        s for s in _latest_snapshots(contract.underlying, contract.expiry) if s.contract_id == contract.pk
    ]
    latest_snapshot = contract_snapshots[0] if contract_snapshots else None
    option_ltp = float(latest_snapshot.ltp) if latest_snapshot and latest_snapshot.ltp is not None else None

    greeks = compute_greeks_for_contract(contract, spot, option_ltp) if option_ltp is not None else None
    if greeks is not None:
        vector.update({"delta": greeks["delta"], "theta": greeks["theta"], "vega": greeks["vega"], "gamma": greeks["gamma"]})

    if latest_snapshot is not None and latest_snapshot.iv is not None:
        history = [s.iv for s in contract_snapshots if s.iv is not None]
        iv_rank = compute_iv_rank(latest_snapshot.iv, history) if history else None
        vector["ivScore"] = round(iv_rank / 100, 4) if iv_rank is not None else None

    # apps.options.confirmation's evaluators expect RAW slope/macd_hist
    # values (dividing by ind["close"] themselves) -- TradingSignal only
    # stores the already-normalized *_pct fields, so multiplying back by
    # `spot` here exactly reconstructs the raw value (multiply/divide
    # cancel exactly), matching apps.options.final_signal's identical
    # reconstruction for the same reason.
    ind = {
        "close": spot,
        "ema9_slope": signal.ind_ema9_slope_pct * spot if signal.ind_ema9_slope_pct is not None else None,
        "ema21_slope": signal.ind_ema21_slope_pct * spot if signal.ind_ema21_slope_pct is not None else None,
        "macd_hist": signal.ind_macd_hist_pct * spot if signal.ind_macd_hist_pct is not None else None,
        "relative_volume": signal.ind_relative_volume,
    }

    risk_reward = None
    if signal.entry_price is not None and signal.stop_loss is not None and signal.target_1 is not None:
        risk = abs(float(signal.entry_price) - float(signal.stop_loss))
        reward = abs(float(signal.target_1) - float(signal.entry_price))
        risk_reward = round(reward / risk, 4) if risk else None

    from .confirmation import evaluate_multi_signal_confirmation
    from .signal_scoring import calculate_signal_score

    confirmation_result = evaluate_multi_signal_confirmation(
        contract.underlying, contract.expiry, direction, ind, spot,
        delta=greeks["delta"] if greeks else None, liquidity_score=None, risk_reward=risk_reward,
    )
    scoring = calculate_signal_score(confirmation_result)
    per_factor = scoring["per_factor_score"]

    vector.update({
        "trendScore": per_factor.get("trend"), "momentumScore": per_factor.get("momentum"),
        "oiScore": per_factor.get("oi"), "volumeScore": per_factor.get("volume"),
        "skewScore": per_factor.get("skew"),
        # No standalone gamma-favorability factor exists in apps.options.
        # confirmation -- this reuses the same combined "greeks" setup-
        # quality score (delta-sweet-spot fit) rather than inventing a
        # second, unrelated gamma-specific scoring rule.
        "gammaScore": per_factor.get("greeks"),
        "liquidityScore": per_factor.get("liquidity"),
        "expectedMoveScore": per_factor.get("expected_move"),
        "riskRewardScore": per_factor.get("risk_reward"),
    })
    return vector


def save_signal_feature_snapshot(signal):
    """
    Best-effort: builds and persists a SignalFeatureSnapshot for this
    signal. NEVER raises -- an audit-trail write failing must never be
    mistaken for a signal-generation failure, the same discipline apps.
    admin_tools.audit.log_action's own docstring establishes for its
    identical category of "logging, not decision-making" write.
    """
    from apps.signals.models import SignalFeatureSnapshot

    try:
        features = build_feature_vector(signal)
        snapshot, _ = SignalFeatureSnapshot.objects.update_or_create(
            signal=signal, defaults={"features": features},
        )
        return snapshot
    except Exception:
        logger.exception(
            "Failed to build/save SignalFeatureSnapshot for signal %s -- signal itself was still saved.",
            signal.pk,
        )
        return None
