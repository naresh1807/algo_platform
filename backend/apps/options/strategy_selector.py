"""
Strategy Selection Engine: classifies which strategy shape a setup
resembles from regime + IV rank + expected-move position + direction --
NOT forced into "BUY CE / BUY PE" for every market condition.

SCOPE LIMIT, stated up front (matches this platform's own execution
capability): apps.execution.paper_executor / live_executor / apps.risk.
engine only understand a SINGLE-LEG position today (OpenPosition has no
multi-leg concept anywhere in this codebase). Building real multi-leg
execution/P&L/risk (spreads, straddles, iron condors) is deliberately a
SEPARATE, future phase, not attempted here. So while this classifier
can NAME any of the strategies below, only LONG_CALL/LONG_PUT/NO_TRADE
are ever "executable": True -- every other classification is reported
purely informationally (what a more complete options desk would
consider here), "executable": False, with an explicit reason, never
silently attempted through infrastructure that doesn't support it.
"""

from __future__ import annotations

EXECUTABLE_STRATEGIES = frozenset({"LONG_CALL", "LONG_PUT", "NO_TRADE"})

# Documented, tunable thresholds -- not backtested/calibrated yet
# (same "assumption stated plainly" posture as apps.options.greeks'
# DEFAULT_RISK_FREE_RATE).
HIGH_IV_RANK_THRESHOLD = 70.0
LOW_IV_RANK_THRESHOLD = 30.0

TRENDING_REGIMES = frozenset({"TRENDING", "TRENDING_BULLISH", "TRENDING_BEARISH"})


def classify_strategy(
    direction: str | None, regime: str | None, iv_rank: float | None,
    expected_move_position: str | None = None, conflict_level: str | None = None,
) -> dict:
    """
    direction: "bullish"|"bearish"|None. regime: a base MarketRegime
    string OR an apps.market_data.multi_timeframe_regime composite
    string -- TRENDING_REGIMES covers both. iv_rank: 0-100 from apps.
    options.metrics.compute_iv_rank, or None. expected_move_position:
    apps.options.expected_move.classify_price_vs_expected_range's
    output, or None. conflict_level: apps.options.confirmation.
    detect_signal_conflict's output, or None.

    Returns {"strategy": str, "executable": bool, "reason": str}.
    """
    if direction is None:
        return {"strategy": "NO_TRADE", "executable": True, "reason": "No clear directional bias to build a strategy around."}
    if conflict_level == "CONFLICT_HIGH":
        return {"strategy": "NO_TRADE", "executable": True, "reason": "Signal conflict is high -- directional factors disagree with each other."}

    is_trending = regime in TRENDING_REGIMES
    is_high_iv = iv_rank is not None and iv_rank >= HIGH_IV_RANK_THRESHOLD
    is_low_iv = iv_rank is not None and iv_rank <= LOW_IV_RANK_THRESHOLD

    # A directional move that's already fully played out (per the
    # expected-move range) softens the case for a fresh directional
    # long option even in a trending regime -- the easy part of the
    # move may already be behind it.
    move_already_extended = (
        (direction == "bullish" and expected_move_position in ("outside_upper_range", "near_upper_range")) or
        (direction == "bearish" and expected_move_position in ("outside_lower_range", "near_lower_range"))
    )

    if is_trending and not is_high_iv and not move_already_extended:
        strategy = "LONG_CALL" if direction == "bullish" else "LONG_PUT"
        return {
            "strategy": strategy, "executable": True,
            "reason": f"Trending, direction={direction}, IV not elevated, room left within the expected move -- a long option is a reasonable, directly executable way to express this.",
        }

    if is_trending and (is_high_iv or move_already_extended):
        strategy = "BULL_CALL_SPREAD" if direction == "bullish" else "BEAR_PUT_SPREAD"
        why = f"IV rank {iv_rank:.0f} is elevated" if is_high_iv else "the expected move already looks largely played out"
        return {
            "strategy": strategy, "executable": False,
            "reason": f"Trending {direction}, but {why} -- a debit spread would cap the cost/risk here, but multi-leg execution isn't built in this platform yet (see module docstring). No fallback long-option trade is substituted.",
        }

    if not is_trending and is_high_iv:
        return {
            "strategy": "IRON_CONDOR", "executable": False,
            "reason": f"Non-trending regime with elevated IV rank {iv_rank:.0f} -- a defined-risk premium-selling structure fits textbook, but multi-leg execution isn't built in this platform yet.",
        }

    if not is_trending and is_low_iv:
        return {
            "strategy": "STRADDLE", "executable": False,
            "reason": f"Non-trending regime with low IV rank {iv_rank:.0f} -- cheap premium favors a volatility-expansion structure, but multi-leg execution isn't built in this platform yet.",
        }

    return {"strategy": "NO_TRADE", "executable": True, "reason": "No sufficiently clear regime/IV combination to justify a specific strategy."}
