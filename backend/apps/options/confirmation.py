"""
Multi-Signal Confirmation Engine + Conflict Detection.

Replaces "one indicator dominates the decision" with an explicit,
per-factor breakdown split into two conceptually different groups --
conflating them into one undifferentiated bullish/bearish list would
itself be a kind of pseudo-science (claiming every factor makes the
same kind of directional claim when several genuinely don't):

  DIRECTIONAL factors (trend, momentum, oi, volume, skew) -- read the
  MARKET's own directional bias. Each reports "bullish"/"bearish"/
  "neutral"/"unavailable".

  SETUP-QUALITY factors (greeks, liquidity, expected_move, risk_reward)
  -- read whether THIS PARTICULAR TRADE CONSTRUCTION is favorable, not
  which way the market is leaning (a well-liquid contract with a good
  risk/reward doesn't mean "the market is bullish" -- it means "if the
  direction call is right, this is a reasonable way to express it").
  Each reports "favorable"/"unfavorable"/"neutral"/"unavailable".

detect_signal_conflict() only ever looks at the DIRECTIONAL group --
"the risk/reward is mediocre" is a reason to size down or skip a trade
(already handled by apps.risk.engine and the pipeline's own reasons/
NO_TRADE machinery), it is not evidence the underlying/options-flow
story CONTRADICTS itself, which is what conflict detection is for.
"""

from __future__ import annotations

from datetime import date


def _evaluate_trend(ind: dict | None, direction: str) -> dict:
    if not ind or ind.get("ema9_slope") is None or ind.get("ema21_slope") is None or not ind.get("close"):
        return {"signal": "unavailable", "detail": "No indicator data."}
    close = ind["close"] or 1.0
    ema9_pct = ind["ema9_slope"] / close
    ema21_pct = ind["ema21_slope"] / close
    if ema9_pct > 0 and ema21_pct > 0:
        signal = "bullish"
    elif ema9_pct < 0 and ema21_pct < 0:
        signal = "bearish"
    else:
        signal = "neutral"
    return {"signal": signal, "detail": f"EMA9 slope {ema9_pct * 100:+.2f}%, EMA21 slope {ema21_pct * 100:+.2f}%"}


def _evaluate_momentum(ind: dict | None, direction: str) -> dict:
    if not ind or ind.get("macd_hist") is None or not ind.get("close"):
        return {"signal": "unavailable", "detail": "No indicator data."}
    macd_hist_pct = ind["macd_hist"] / (ind["close"] or 1.0)
    if macd_hist_pct > 0:
        signal = "bullish"
    elif macd_hist_pct < 0:
        signal = "bearish"
    else:
        signal = "neutral"
    return {"signal": signal, "detail": f"MACD histogram {macd_hist_pct * 100:+.3f}% of price"}


def _evaluate_oi(underlying: str, expiry: date, option_type: str, direction: str) -> dict:
    from .oi_intelligence import classify_buildup_with_confidence

    result = classify_buildup_with_confidence(underlying, expiry, option_type)
    if result["classification"] is None:
        return {"signal": "unavailable", "detail": result["detail"]}
    bullish_classifications = {"buildup_bullish", "short_covering"}
    signal = "bullish" if result["classification"] in bullish_classifications else "bearish"
    return {"signal": signal, "detail": result["detail"]}


def _evaluate_volume(ind: dict | None, direction: str) -> dict:
    """
    Volume alone has no inherent direction -- it's read together with
    the price direction it's accompanying: elevated volume (relative_
    volume > 1.2) alongside a price move in the SAME direction as the
    move confirms it; elevated volume against the proposed direction
    means the crowd is actually moving the other way harder than
    expected. Normal/low volume is neutral either way -- it neither
    confirms nor contradicts.
    """
    if not ind or ind.get("relative_volume") is None or ind.get("macd_hist") is None:
        return {"signal": "unavailable", "detail": "No indicator data."}
    relative_volume = ind["relative_volume"]
    if relative_volume <= 1.2:
        return {"signal": "neutral", "detail": f"Relative volume {relative_volume:.2f}x -- not elevated enough to read."}
    price_direction = "bullish" if ind["macd_hist"] > 0 else "bearish" if ind["macd_hist"] < 0 else "neutral"
    if price_direction == "neutral":
        return {"signal": "neutral", "detail": f"Relative volume {relative_volume:.2f}x elevated, but no clear price direction to attach it to."}
    return {"signal": price_direction, "detail": f"Relative volume {relative_volume:.2f}x elevated, accompanying a {price_direction} price move."}


def _evaluate_skew(underlying: str, expiry: date, spot: float, direction: str) -> dict:
    """
    Simple, documented heuristic (not backtested/calibrated yet --
    same "assumption stated plainly" posture as apps.options.greeks'
    DEFAULT_RISK_FREE_RATE): index options normally price puts richer
    than calls (positive 25-delta skew is the typical/neutral state).
    A skew that's flipped negative (calls unusually rich) reads as a
    mild bullish tilt (aggressive call demand pushing call IV up); a
    skew well above the normal band reads as a mild bearish tilt
    (elevated hedging/put demand). The exact band edges below are a
    starting point, not a validated threshold -- revisit once there's
    real historical skew data to calibrate against.
    """
    from .volatility_surface import calculate_25_delta_skew

    skew = calculate_25_delta_skew(underlying, expiry, spot)
    if skew is None:
        return {"signal": "unavailable", "detail": "25-delta skew unavailable."}
    if skew < 0:
        signal = "bullish"
    elif skew > 3.0:  # points of IV -- a documented starting threshold, not a calibrated one
        signal = "bearish"
    else:
        signal = "neutral"
    return {"signal": signal, "detail": f"25-delta skew (put IV - call IV) = {skew:+.2f} points"}


def _evaluate_greeks(delta: float | None) -> dict:
    if delta is None:
        return {"signal": "unavailable", "detail": "No delta provided."}
    from .strike_selector import DELTA_SWEET_SPOT

    in_sweet_spot = DELTA_SWEET_SPOT[0] <= abs(delta) <= DELTA_SWEET_SPOT[1]
    return {
        "signal": "favorable" if in_sweet_spot else "unfavorable",
        "detail": f"delta {delta:+.2f} ({'inside' if in_sweet_spot else 'outside'} the {DELTA_SWEET_SPOT} sweet spot)",
    }


def _evaluate_liquidity(liquidity_score: float | None) -> dict:
    if liquidity_score is None:
        return {"signal": "unavailable", "detail": "No liquidity score provided."}
    return {"signal": "favorable" if liquidity_score >= 0.6 else "unfavorable", "detail": f"liquidity score {liquidity_score:.2f}"}


def _evaluate_expected_move(underlying: str, expiry: date, spot: float, direction: str) -> dict:
    from .expected_move import calculate_expected_move_for_contract, classify_price_vs_expected_range

    em = calculate_expected_move_for_contract(underlying, expiry, spot)
    if em["upper_range"] is None:
        return {"signal": "unavailable", "detail": "Expected move unavailable (no ATM IV yet)."}

    position = classify_price_vs_expected_range(spot, em["upper_range"], em["lower_range"])
    if position == "inside_range":
        signal = "favorable"  # room left to move in either direction
    elif (direction == "bullish" and position in ("outside_upper_range", "near_upper_range")) or \
         (direction == "bearish" and position in ("outside_lower_range", "near_lower_range")):
        signal = "unfavorable"  # the move this trade needs has largely already happened
    else:
        signal = "neutral"
    return {"signal": signal, "detail": f"spot is {position.replace('_', ' ')} (expected move +/-{em['expected_move']})"}


def _evaluate_risk_reward(risk_reward: float | None) -> dict:
    if risk_reward is None:
        return {"signal": "unavailable", "detail": "No risk/reward provided."}
    return {"signal": "favorable" if risk_reward >= 1.5 else "unfavorable", "detail": f"risk/reward 1:{risk_reward:.2f}"}


def detect_signal_conflict(directional_factors: dict, direction: str) -> tuple[str, str]:
    """
    Counts how many AVAILABLE directional factors actively disagree
    with the proposed direction (the opposite bullish/bearish label --
    "neutral"/"unavailable" is neither agreement nor disagreement).
    CONFLICT_HIGH if half or more of the available factors disagree,
    CONFLICT_MEDIUM if at least one does, CONFLICT_LOW otherwise
    (including when there's simply nothing available to conflict with
    yet -- that's an honest "can't tell", not a green light, and the
    caller's own data-quality/no-trade gates handle "not enough data"
    separately from "the data that exists disagrees").
    """
    opposite = "bearish" if direction == "bullish" else "bullish"
    disagreeing_names = [name for name, f in directional_factors.items() if f["signal"] == opposite]
    available_count = sum(1 for f in directional_factors.values() if f["signal"] != "unavailable")

    if available_count == 0:
        return "CONFLICT_LOW", "No directional factors available to compare -- nothing to conflict with."

    disagreement_ratio = len(disagreeing_names) / available_count
    if disagreement_ratio >= 0.5:
        level = "CONFLICT_HIGH"
    elif disagreeing_names:
        level = "CONFLICT_MEDIUM"
    else:
        level = "CONFLICT_LOW"

    if disagreeing_names:
        detail = f"{len(disagreeing_names)}/{available_count} available directional factors disagree with the {direction} thesis: {', '.join(disagreeing_names)}."
    else:
        detail = f"All {available_count} available directional factors agree with or are neutral to the {direction} thesis."
    return level, detail


def evaluate_multi_signal_confirmation(
    underlying: str, expiry: date, direction: str, ind: dict | None, spot: float,
    delta: float | None = None, liquidity_score: float | None = None, risk_reward: float | None = None,
) -> dict:
    """
    The composed entry point: builds both factor groups and runs
    conflict detection over the directional group. delta/liquidity_
    score/risk_reward are optional -- pass them when the caller (apps.
    options.index_direction_strategy, after resolving a contract)
    already has them computed; this function COMBINES signals, it
    doesn't re-derive contract-specific ones from scratch.
    """
    option_type = "CE" if direction == "bullish" else "PE"

    directional_factors = {
        "trend": _evaluate_trend(ind, direction),
        "momentum": _evaluate_momentum(ind, direction),
        "oi": _evaluate_oi(underlying, expiry, option_type, direction),
        "volume": _evaluate_volume(ind, direction),
        "skew": _evaluate_skew(underlying, expiry, spot, direction),
    }
    setup_quality_factors = {
        "greeks": _evaluate_greeks(delta),
        "liquidity": _evaluate_liquidity(liquidity_score),
        "expected_move": _evaluate_expected_move(underlying, expiry, spot, direction),
        "risk_reward": _evaluate_risk_reward(risk_reward),
    }
    conflict_level, conflict_detail = detect_signal_conflict(directional_factors, direction)

    return {
        "directional_factors": directional_factors,
        "setup_quality_factors": setup_quality_factors,
        "conflict_level": conflict_level,
        "conflict_detail": conflict_detail,
    }
