"""
AISignalService -- the restricted action-space decision policy.
Heuristic-first (always functional, even before any model exists),
blends toward the active apps.learning.ModelRegistry(model_name=
"paper_trading_policy") champion once one has been promoted by
model_promotion_service. A model load/predict failure NEVER blocks
trading -- it just falls through to the heuristic, same "never let an
analytics failure block the real decision" posture
apps.options.signals.py's Greeks calc already uses elsewhere.

Quantity is NEVER decided here -- always exactly one lot, enforced
downstream by paper_risk_engine before any order is submitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models import Action
from . import contract_selection, explainability, feature_engineering, paper_risk_engine

HEURISTIC_WEIGHTS = {
    "ema9_slope": 1.0, "ema21_slope": 0.6, "macd_hist": 1.2,
    "rsi": 0.5, "adx_trend": 0.8, "relative_volume": 0.4, "ema9_streak": 0.3,
}
BULLISH_THRESHOLD = 1.5
BEARISH_THRESHOLD = -1.5
ATR_STOP_MULTIPLIER = 0.75
TARGET_RISK_REWARD = Decimal("2")

# Scalp mode (decide_scalp_direction/evaluate_scalp_entry below): tighter
# target than swing's 2R, matching how apps.learning.strategy_methods'
# own scalping ideas are actually meant to be traded (quick 1-1.5R, not
# a multi-candle swing target). SCALP_MIN/MAX_STOP_RATIO bound the
# option-premium stop distance derived from the firing idea's own
# underlying-level risk (see evaluate_scalp_entry) -- a sanity floor/
# ceiling, not a tuned parameter.
SCALP_TARGET_RISK_REWARD = Decimal("1.5")
SCALP_MIN_STOP_RATIO = Decimal("0.05")
SCALP_MAX_STOP_RATIO = Decimal("0.25")
SCALP_DEFAULT_STOP_RATIO = Decimal("0.10")


@dataclass
class AIDecisionResult:
    action: str
    confidence: float
    expected_return: float | None
    reason: str
    explanation: dict
    contract_candidate: object | None = None
    selected_stop: Decimal | None = None
    selected_target: Decimal | None = None
    model_version: str = ""
    features: dict | None = None
    # Scalp mode only: the firing idea's own underlying-level
    # (entry-stop)/entry ratio, carried from decide_scalp_direction to
    # evaluate_scalp_entry so the option-premium stop can be sized off
    # THIS idea's actual risk profile (SAR-burst's raw SAR distance isn't
    # a fixed ATR multiple the way the other two scalp methods are)
    # instead of a flat multiplier. Never stored in `features` itself --
    # that dict is the exact apps.learning.ModelRegistry feature schema
    # (see feature_engineering.FEATURE_SCHEMA_VERSION) and must stay
    # identical in shape whether a decision came from swing or scalp mode.
    stop_distance_ratio: Decimal | None = None


def _heuristic_score(features: dict) -> tuple[float, dict]:
    scores: dict[str, float] = {}
    scores["ema9_slope"] = HEURISTIC_WEIGHTS["ema9_slope"] * (1 if (features.get("ema9_slope") or 0) > 0 else -1)
    scores["ema21_slope"] = HEURISTIC_WEIGHTS["ema21_slope"] * (1 if (features.get("ema21_slope") or 0) > 0 else -1)
    macd_hist = features.get("macd_hist") or 0
    if macd_hist:
        scores["macd_hist"] = HEURISTIC_WEIGHTS["macd_hist"] * (1 if macd_hist > 0 else -1) * min(1.0, abs(macd_hist) * 10)
    rsi = features.get("rsi")
    if rsi is not None:
        if rsi > 55:
            scores["rsi"] = HEURISTIC_WEIGHTS["rsi"]
        elif rsi < 45:
            scores["rsi"] = -HEURISTIC_WEIGHTS["rsi"]
    adx = features.get("adx") or 0
    if adx >= 25:
        trend_dir = 1 if (features.get("ema9_slope") or 0) > 0 else -1
        scores["adx_trend"] = HEURISTIC_WEIGHTS["adx_trend"] * trend_dir
    rel_vol = features.get("relative_volume")
    if rel_vol is not None and rel_vol > 1.2:
        scores["relative_volume"] = HEURISTIC_WEIGHTS["relative_volume"] * (1 if (features.get("candle_return_pct") or 0) > 0 else -1)
    if (features.get("close_above_ema9_streak") or 0) >= 2:
        scores["ema9_streak"] = HEURISTIC_WEIGHTS["ema9_streak"]
    if (features.get("close_below_ema9_streak") or 0) >= 2:
        scores["ema9_streak"] = -HEURISTIC_WEIGHTS["ema9_streak"]
    return sum(scores.values()), scores


def _active_policy_registry_row():
    from apps.learning.models import ModelRegistry

    return ModelRegistry.objects.filter(model_name="paper_trading_policy", active_flag=True).first()


def decide_direction(underlying: str, timeframe: str) -> AIDecisionResult:
    """Stage 1: HOLD, or a directional lean the caller then tries to turn
    into a concrete CE/PE candidate."""
    features = feature_engineering.build_underlying_features(underlying, timeframe)
    if features is None:
        empty = {"method": "unavailable", "positive_factors": [], "negative_factors": []}
        return AIDecisionResult(Action.HOLD, 0.0, None, "not enough candle history yet", empty)

    registry_row = _active_policy_registry_row()
    if registry_row is not None:
        try:
            import joblib

            ensemble = joblib.load(registry_row.artifact_path)
            feature_names = registry_row.metrics_json.get("feature_columns", [])
            vector = [float(features.get(name) or 0.0) for name in feature_names]
            win_prob = float(ensemble.predict_proba([vector])[0][1])
            explanation = explainability.explain_model(ensemble, vector, feature_names)
            heuristic_score, _ = _heuristic_score(features)
            direction = "CE" if heuristic_score > 0 else ("PE" if heuristic_score < 0 else None)
            if win_prob < 0.5 or direction is None:
                return AIDecisionResult(
                    Action.HOLD, win_prob, None, f"model win-probability {win_prob:.2f} below threshold",
                    explanation, model_version=registry_row.model_version, features=features,
                )
            action = Action.BUY_CE_ONE_LOT if direction == "CE" else Action.BUY_PE_ONE_LOT
            return AIDecisionResult(
                action, win_prob, win_prob - 0.5,
                f"model win-probability {win_prob:.2f}, heuristic confirms {direction}",
                explanation, model_version=registry_row.model_version, features=features,
            )
        except Exception:
            pass  # model unusable this tick -- fall through to the always-available heuristic

    heuristic_score, rule_scores = _heuristic_score(features)
    explanation = explainability.explain_heuristic(rule_scores)
    confidence = min(1.0, abs(heuristic_score) / 5.0)
    if heuristic_score >= BULLISH_THRESHOLD:
        action, reason = Action.BUY_CE_ONE_LOT, f"heuristic bullish score {heuristic_score:.2f}"
    elif heuristic_score <= BEARISH_THRESHOLD:
        action, reason = Action.BUY_PE_ONE_LOT, f"heuristic bearish score {heuristic_score:.2f}"
    else:
        action, reason = Action.HOLD, f"heuristic score {heuristic_score:.2f} inside neutral band"
    return AIDecisionResult(action, confidence, heuristic_score, reason, explanation, features=features)


def evaluate_entry(account, underlying: str, timeframe: str) -> AIDecisionResult:
    """Full pipeline: direction -> contract selection -> hard risk gate ->
    ATR-bounded stop/target. Returns HOLD (no directional edge) or
    SKIP_SIGNAL (had a directional lean but couldn't safely trade it) or
    a ready-to-execute BUY_CE_ONE_LOT/BUY_PE_ONE_LOT result."""
    from apps.market_data.indicators import compute_indicators

    result = decide_direction(underlying, timeframe)
    if result.action == Action.HOLD:
        return result

    option_type = "CE" if result.action == Action.BUY_CE_ONE_LOT else "PE"
    candidate, selection_reason = contract_selection.select_contract(underlying, option_type)
    if candidate is None:
        return AIDecisionResult(
            Action.SKIP_SIGNAL, result.confidence, result.expected_return,
            f"no valid contract: {selection_reason}", result.explanation,
            model_version=result.model_version, features=result.features,
        )

    risk_decision = paper_risk_engine.check_pre_entry(
        account, candidate.contract, ltp=candidate.quote.ltp, bid=candidate.quote.bid, ask=candidate.quote.ask,
        open_interest=candidate.quote.open_interest, volume=candidate.quote.volume, feed_age_seconds=candidate.quote.age_seconds,
    )
    if not risk_decision.approved:
        return AIDecisionResult(
            Action.SKIP_SIGNAL, result.confidence, result.expected_return,
            f"risk-blocked: {risk_decision.reason_text}", result.explanation,
            contract_candidate=candidate, model_version=result.model_version, features=result.features,
        )

    underlying_indicators = compute_indicators(underlying, timeframe) or {}
    atr = underlying_indicators.get("atr") or 0.0
    entry_ref = Decimal(str(candidate.quote.ask if candidate.quote.ask is not None else candidate.quote.ltp))
    # Stop distance floors at 15% of premium when ATR is unavailable/tiny
    # (an option's own ATR isn't tracked; this uses the UNDERLYING's ATR
    # scaled down, a documented approximation) -- the max-hard-risk
    # boundary itself is enforced later by paper_risk_engine's account-
    # level checks and position_service.tighten_stop's no-widen rule,
    # not here.
    stop_distance = Decimal(str(atr)) * Decimal(str(ATR_STOP_MULTIPLIER)) if atr else entry_ref * Decimal("0.15")
    stop_distance = max(stop_distance, entry_ref * Decimal("0.10"))
    selected_stop = max(Decimal("0.05"), entry_ref - stop_distance)
    selected_target = entry_ref + stop_distance * TARGET_RISK_REWARD

    result.contract_candidate = candidate
    result.selected_stop = selected_stop
    result.selected_target = selected_target
    return result


def decide_scalp_direction(underlying: str) -> AIDecisionResult:
    """Scalp-mode sibling of decide_direction: sources direction from
    apps.learning.strategy_methods.SCALPING_METHOD_FUNCS on 1-minute
    candles (the same SAR+volume-burst / EMA-momentum / RSI-extreme idea
    generators apps.learning.scalp_execution already trades for real,
    reused here rather than re-implemented) instead of decide_direction's
    5m weighted heuristic score. All three scalp ideas are long-only (see
    strategy_methods.py's own module docstring) so a fired idea always
    means CE, never PE -- same reason apps.learning.scalp_execution
    hardcodes side="CE".

    Reuses the exact same champion-model-then-heuristic-fallback
    structure and the exact same win_prob < 0.5 confidence gate as
    decide_direction (same apps.learning.ModelRegistry(model_name=
    "paper_trading_policy") row, same feature_engineering feature
    schema, just built on 1m candles) -- an idea firing is necessary but
    not sufficient, exactly as clearing BULLISH_THRESHOLD isn't the
    final word once a champion model exists. This keeps scalp entries
    subject to the identical confidence discipline as swing entries
    instead of re-introducing an ungated fast-path.
    """
    from apps.learning.strategy_methods import SCALPING_METHOD_FUNCS
    from apps.signals.engine import _evaluate_buy_conditions

    fired = None
    for method_name, generate_idea in SCALPING_METHOD_FUNCS.items():
        idea = generate_idea(underlying, "1m")
        if idea is not None and idea.get("ind") is not None:
            fired = (method_name, idea)
            break

    if fired is None:
        empty = {"method": "unavailable", "positive_factors": [], "negative_factors": []}
        return AIDecisionResult(Action.HOLD, 0.0, None, "no scalp method fired this cycle (1m)", empty)

    method_name, idea = fired
    idea_entry = idea["entry_price"]
    idea_stop = idea["stop_loss"]
    stop_distance_ratio = (
        Decimal(str(abs(idea_entry - idea_stop) / idea_entry))
        if idea_entry else SCALP_DEFAULT_STOP_RATIO
    )

    features = feature_engineering.build_underlying_features(underlying, "1m")
    if features is None:
        empty = {"method": "unavailable", "positive_factors": [], "negative_factors": []}
        return AIDecisionResult(
            Action.HOLD, 0.0, None,
            f"scalp method '{method_name}' fired but not enough 1m candle history for features yet", empty,
        )
    features = {**features, "scalp_method": method_name}

    registry_row = _active_policy_registry_row()
    if registry_row is not None:
        try:
            import joblib

            ensemble = joblib.load(registry_row.artifact_path)
            feature_names = registry_row.metrics_json.get("feature_columns", [])
            vector = [float(features.get(name) or 0.0) for name in feature_names]
            win_prob = float(ensemble.predict_proba([vector])[0][1])
            explanation = explainability.explain_model(ensemble, vector, feature_names)
            if win_prob < 0.5:
                return AIDecisionResult(
                    Action.HOLD, win_prob, None,
                    f"scalp method '{method_name}' fired but model win-probability {win_prob:.2f} below threshold",
                    explanation, model_version=registry_row.model_version, features=features,
                )
            return AIDecisionResult(
                Action.BUY_CE_ONE_LOT, win_prob, win_prob - 0.5,
                f"scalp method '{method_name}' fired, model win-probability {win_prob:.2f}",
                explanation, model_version=registry_row.model_version, features=features,
                stop_distance_ratio=stop_distance_ratio,
            )
        except Exception:
            pass  # model unusable this tick -- fall through to the always-available heuristic

    ind = idea["ind"]
    buy_conditions = _evaluate_buy_conditions(ind)
    technical_score = round(sum(buy_conditions.values()) / len(buy_conditions), 4)
    explanation = explainability.explain_heuristic({k: (1.0 if v else -1.0) for k, v in buy_conditions.items()})
    return AIDecisionResult(
        Action.BUY_CE_ONE_LOT, technical_score, technical_score - 0.5,
        f"scalp method '{method_name}' fired ({technical_score:.0%} of technical buy conditions also true)",
        explanation, model_version=f"heuristic:scalp:{method_name}", features=features,
        stop_distance_ratio=stop_distance_ratio,
    )


def evaluate_scalp_entry(account, underlying: str) -> AIDecisionResult:
    """Scalp-mode sibling of evaluate_entry: identical contract-selection
    -> hard-risk-gate pipeline (same paper_risk_engine.check_pre_entry,
    same one-lot/one-position enforcement downstream), but sourcing
    direction from decide_scalp_direction instead of decide_direction,
    and a tighter stop/target sized off the firing idea's OWN
    underlying-level risk ratio (see AIDecisionResult.stop_distance_ratio's
    docstring for why a flat ATR multiplier doesn't fit all three scalp
    methods) with SCALP_TARGET_RISK_REWARD instead of evaluate_entry's 2R.
    """
    result = decide_scalp_direction(underlying)
    if result.action == Action.HOLD:
        return result

    # All fired scalp ideas are long-only -> CE (see decide_scalp_direction).
    candidate, selection_reason = contract_selection.select_contract(underlying, "CE")
    if candidate is None:
        return AIDecisionResult(
            Action.SKIP_SIGNAL, result.confidence, result.expected_return,
            f"no valid contract: {selection_reason}", result.explanation,
            model_version=result.model_version, features=result.features,
        )

    risk_decision = paper_risk_engine.check_pre_entry(
        account, candidate.contract, ltp=candidate.quote.ltp, bid=candidate.quote.bid, ask=candidate.quote.ask,
        open_interest=candidate.quote.open_interest, volume=candidate.quote.volume, feed_age_seconds=candidate.quote.age_seconds,
    )
    if not risk_decision.approved:
        return AIDecisionResult(
            Action.SKIP_SIGNAL, result.confidence, result.expected_return,
            f"risk-blocked: {risk_decision.reason_text}", result.explanation,
            contract_candidate=candidate, model_version=result.model_version, features=result.features,
        )

    entry_ref = Decimal(str(candidate.quote.ask if candidate.quote.ask is not None else candidate.quote.ltp))
    risk_ratio = result.stop_distance_ratio or SCALP_DEFAULT_STOP_RATIO
    risk_ratio = max(SCALP_MIN_STOP_RATIO, min(risk_ratio, SCALP_MAX_STOP_RATIO))
    stop_distance = entry_ref * risk_ratio
    selected_stop = max(Decimal("0.05"), entry_ref - stop_distance)
    selected_target = entry_ref + stop_distance * SCALP_TARGET_RISK_REWARD

    result.contract_candidate = candidate
    result.selected_stop = selected_stop
    result.selected_target = selected_target
    return result
