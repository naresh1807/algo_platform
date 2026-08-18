"""
Continuous liquidity scoring -- deliberately separate from apps.risk.
engine.check_option_contract_liquidity, which stays a hard PASS/FAIL
gate using settings.RISK_HARD_LIMITS (never DB-editable, per that
dict's own docstring). This module produces a 0-1 SCORE for RANKING
and COMPARING contracts (apps.options.strike_selector's AI-mode
scoring, and the later signal-scoring engine) -- "how liquid, relative
to other candidates" is a different question than "liquid enough to
trade at all," and the risk gate already answers the second one.

Every component score is anchored to the SAME reference thresholds the
hard gate uses (settings.RISK_HARD_LIMITS), so "spread_score == 0"
genuinely means "this contract would already fail the hard gate," not
an arbitrary second opinion.
"""

from __future__ import annotations

from django.conf import settings

# No project-wide minimum-volume setting exists by default
# (MIN_OPTION_VOLUME defaults to 0, i.e. off, per apps.risk.engine's
# own liquidity gate) -- this is a separate, purely descriptive
# reference point for the CONTINUOUS score only (never used to block a
# trade), documented here since it isn't already defined anywhere else.
VOLUME_SCORE_REFERENCE = 100


def calculate_liquidity_score(
    bid: float | None, ask: float | None, open_interest: int | None, volume: int | None,
) -> dict:
    """
    Returns {"liquidity_score": 0..1 | None, "spread_score", "oi_score",
    "volume_score", "spread_pct"} -- each component score None if its
    own input is missing (never guessed), and the overall score None if
    ANY required component is missing (an overall score built from
    partial data would be misleading, not merely imprecise).

    Weights (spread 40%, OI 35%, volume 25%) are documented constants
    here, not hidden inside the arithmetic -- tune these three numbers
    in one place if backtesting later shows a different weighting
    predicts realized slippage/fill-quality better.
    """
    limits = settings.RISK_HARD_LIMITS
    max_spread_pct = limits["MAX_OPTION_BID_ASK_SPREAD_PCT"]
    min_oi_reference = limits["MIN_OPTION_OPEN_INTEREST"]

    spread_score = spread_pct = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2
        if mid > 0:
            spread_pct = (ask - bid) / mid * 100
            spread_score = max(0.0, min(1.0, 1 - spread_pct / max_spread_pct)) if max_spread_pct > 0 else None

    oi_score = None
    if open_interest is not None and min_oi_reference > 0:
        oi_score = round(open_interest / (open_interest + min_oi_reference), 4)
    elif open_interest is not None:
        oi_score = 1.0 if open_interest > 0 else 0.0

    volume_score = None
    if volume is not None:
        volume_score = round(volume / (volume + VOLUME_SCORE_REFERENCE), 4)

    components = [spread_score, oi_score, volume_score]
    weights = [0.40, 0.35, 0.25]
    if any(c is None for c in components):
        liquidity_score = None
    else:
        liquidity_score = round(sum(c * w for c, w in zip(components, weights)), 4)

    return {
        "liquidity_score": liquidity_score,
        "spread_score": spread_score, "oi_score": oi_score, "volume_score": volume_score,
        "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
    }


def estimate_slippage(bid: float | None, ask: float | None) -> float | None:
    """
    Conservative, depth-unaware slippage estimate: half the quoted
    bid/ask spread -- the standard "assume a market order crosses to
    the far side of a THIN book" proxy when only top-of-book bid/ask is
    available (no market-depth/order-book data exists in this platform
    -- see apps.options.order_flow's own module docstring for that
    gap). This deliberately does NOT scale by order size (that would
    need real depth to justify), so treat it as a floor/reference
    estimate for a typical small order, not a precise prediction for
    every possible quantity.
    """
    if bid is None or ask is None:
        return None
    return round((ask - bid) / 2, 4)
