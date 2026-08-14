"""
Fundamental quality score (0-100) + a suggested holding period, from
real FundamentalSnapshot rows (apps.investing.fundamentals_client's
actual fetched data, never fabricated numbers) -- COMBINED with
valuation (apps.investing.valuation: is the price cheap, fair, or
expensive relative to the stock's own history) and technical trend
(apps.investing.technical_score: daily moving-average/RSI read) into
one composite score, per the trader's explicit request that the
suggestion be based on all three together, not fundamentals alone.

IMPORTANT, stated plainly because this output will be shown directly
to a trader making real money decisions: this is a heuristic screen
over public financial ratios, price history, and technical indicators
-- not investment advice, not a guarantee, and not a substitute for
reading the actual results/annual report yourself. It cannot see
management quality, competitive moat, litigation risk, or sector
cyclicality. score_stock() always includes that caveat in the returned
reasoning text itself, not only in this docstring, so it survives
being read on its own (a dashboard card, a JARVIS response) without
this comment.

Weights and thresholds below are a documented ASSUMPTION, not a
manual-specified formula (no manual chapter covers long-term equity
investing at all -- this app exists because the trader asked for it
directly). Anyone using this should feel free to tune WEIGHTS and the
HOLD_MONTHS bands in one place, right here, rather than needing to hunt
through the scoring logic itself.
"""

from __future__ import annotations

from datetime import date

# Each factor's weight in the composite score. Sums to 100 when every
# factor is available; when some are missing (a stock with only 2
# reported quarters won't have a meaningful YoY figure yet, say),
# score_stock() renormalizes over whichever factors ARE available --
# see _weighted_average below.
WEIGHTS = {
    "revenue_growth": 10,
    "profit_growth": 15,
    "growth_consistency": 12,
    "roe": 10,
    "roce": 7,
    "debt_to_equity": 8,
    "promoter_holding": 6,
    "valuation": 16,
    "technical": 16,
}

MIN_FACTORS_REQUIRED = 3  # below this, there's too little data to say anything meaningful

# score -> (verdict, hold-months band). Checked top-down; first match wins.
# Documented assumption, not a formula derived from any backtest --
# these are round, conservative numbers matching how a trader would
# describe conviction levels in plain language ("this is a 5-year
# compounder" vs "watch this one, not ready yet").
_HOLD_BANDS = [
    (80, "strong_hold", (36, 60)),   # 3-5 years
    (65, "strong_hold", (18, 36)),   # 1.5-3 years
    (50, "moderate_hold", (6, 18)),  # 6-18 months
    (35, "watch", None),
    (0, "avoid", None),
]


def _score_growth(value: float | None, strong: float, weak: float) -> float | None:
    """Linear score 0-100: <=weak -> 0, >=strong -> 100, clamped between."""
    if value is None:
        return None
    if value >= strong:
        return 100.0
    if value <= weak:
        return 0.0
    return (value - weak) / (strong - weak) * 100.0


def _score_debt_to_equity(value: float | None) -> float | None:
    """Lower is better -- inverted scale. D/E<=0.3 scores 100, >=2.0 scores 0."""
    if value is None:
        return None
    return _score_growth(-value, strong=-0.3, weak=-2.0)


def _score_growth_consistency(snapshots: list) -> float | None:
    """
    % of the recent quarterly snapshots (up to 4) with positive
    profit_growth_yoy -- a company growing EVERY quarter scores 100; one
    that grew 3 of 4 scores 75; and so on. This is what actually
    distinguishes "one good quarter" from "results are consistently
    good" (the trader's own stated bar), which a single latest-quarter
    figure can't tell you.
    """
    recent = [s for s in snapshots[:4] if s.profit_growth_yoy is not None]
    # Need at least 2 quarters -- with just 1, this collapses into
    # "did the one quarter we have grow" (always 0 or 100), which is
    # exactly the "one good quarter" read this factor exists to be
    # distinguished FROM, and would let a single quarter's
    # profit_growth_yoy double as two "independent" factors toward
    # MIN_FACTORS_REQUIRED (itself, plus this derived from it).
    if len(recent) < 2:
        return None
    positive = sum(1 for s in recent if s.profit_growth_yoy > 0)
    return positive / len(recent) * 100.0


def _weighted_average(factor_scores: dict[str, float | None]) -> tuple[float, int] | None:
    available = {k: v for k, v in factor_scores.items() if v is not None}
    if len(available) < MIN_FACTORS_REQUIRED:
        return None
    total_weight = sum(WEIGHTS[k] for k in available)
    weighted_sum = sum(v * WEIGHTS[k] for k, v in available.items())
    return weighted_sum / total_weight, len(available)


def _verdict_and_hold_months(score: float) -> tuple[str, int | None]:
    for i, (threshold, verdict, months_band) in enumerate(_HOLD_BANDS):
        if score >= threshold:
            if months_band is None:
                return verdict, None
            low, high = months_band
            # Interpolate within the band by how far into it the score
            # sits, so an 82 and a 99 (both "strong_hold") don't get an
            # identical, falsely-precise-looking suggestion. The band's
            # upper boundary is the PREVIOUS (next-higher) threshold in
            # this descending-sorted list -- 100 for the topmost band,
            # since there's no threshold above it.
            band_top = _HOLD_BANDS[i - 1][0] if i > 0 else 100
            span = max(band_top - threshold, 1)
            frac = min((score - threshold) / span, 1.0)
            months = round(low + frac * (high - low))
            return verdict, months
    return "avoid", None  # unreachable given the 0-threshold catch-all above, kept for clarity


def score_stock(stock) -> dict | None:
    """
    stock: an apps.investing.models.Stock. Returns
    {"score", "verdict", "suggested_hold_months", "reasoning",
    "based_on_snapshot", "valuation_label", "technical_trend"} or None
    if there isn't enough real data yet to score it (never fabricates a
    score to fill the gap).
    """
    snapshots = list(stock.fundamentals.filter(reporting_type="quarterly").order_by("-period_end")[:8])
    if not snapshots:
        return None

    latest = snapshots[0]

    from .technical_score import compute_technical_score
    from .valuation import assess_valuation

    valuation = assess_valuation(stock)
    technical = compute_technical_score(stock)

    factor_scores = {
        "revenue_growth": _score_growth(latest.revenue_growth_yoy, strong=20, weak=0),
        "profit_growth": _score_growth(latest.profit_growth_yoy, strong=25, weak=0),
        "growth_consistency": _score_growth_consistency(snapshots),
        "roe": _score_growth(latest.roe, strong=20, weak=5),
        "roce": _score_growth(latest.roce, strong=18, weak=5),
        "debt_to_equity": _score_debt_to_equity(latest.debt_to_equity),
        "promoter_holding": _score_growth(latest.promoter_holding_pct, strong=60, weak=15),
        "valuation": valuation["score"] if valuation else None,
        "technical": technical["score"] if technical else None,
    }

    result = _weighted_average(factor_scores)
    if result is None:
        return None
    score, n_factors = result

    verdict, hold_months = _verdict_and_hold_months(score)
    reasoning = _build_reasoning(
        stock, latest, factor_scores, score, verdict, hold_months, n_factors, valuation, technical,
    )

    return {
        "score": round(score, 1),
        "verdict": verdict,
        "suggested_hold_months": hold_months,
        "reasoning": reasoning,
        "based_on_snapshot": latest,
        "valuation_label": valuation["label"] if valuation else "",
        "technical_trend": technical["trend"] if technical else "",
    }


def _build_reasoning(stock, latest, factor_scores, score, verdict, hold_months, n_factors, valuation, technical) -> str:
    parts = [f"{stock.symbol}: fundamental score {score:.0f}/100 from {n_factors} available factor(s)."]

    detail_bits = []
    if latest.revenue_growth_yoy is not None:
        detail_bits.append(f"revenue growth {latest.revenue_growth_yoy:+.1f}% YoY")
    if latest.profit_growth_yoy is not None:
        detail_bits.append(f"profit growth {latest.profit_growth_yoy:+.1f}% YoY")
    if factor_scores["growth_consistency"] is not None:
        detail_bits.append(f"profit grew in {factor_scores['growth_consistency']:.0f}% of the last few quarters")
    if latest.roe is not None:
        detail_bits.append(f"ROE {latest.roe:.1f}%")
    if latest.roce is not None:
        detail_bits.append(f"ROCE {latest.roce:.1f}%")
    if latest.debt_to_equity is not None:
        detail_bits.append(f"D/E {latest.debt_to_equity:.2f}")
    if latest.promoter_holding_pct is not None:
        detail_bits.append(f"promoter holding {latest.promoter_holding_pct:.1f}%")
    if detail_bits:
        parts.append(", ".join(detail_bits) + ".")

    if valuation:
        parts.append(f"Price: {valuation['label']} -- {valuation['detail']}")
    else:
        parts.append("Price: no P/E data available yet.")

    if technical:
        parts.append(f"Technical: {technical['detail']}")
    else:
        parts.append("Technical: not enough daily price history yet (need 50+ days).")

    if verdict == "strong_hold":
        parts.append(f"Strong long-term hold candidate overall -- roughly {hold_months} months (revisit each quarterly result).")
    elif verdict == "moderate_hold":
        parts.append(f"Moderate hold candidate overall -- roughly {hold_months} months, worth re-checking after the next result.")
    elif verdict == "watch":
        parts.append("Not yet a hold candidate overall -- worth watching, needs stronger fundamentals, a better price, or a better trend first.")
    else:
        parts.append("Fundamentals, valuation, and/or trend don't support a hold right now.")

    parts.append(
        "This is a heuristic screen over public financial ratios, price history, and technical indicators -- "
        "not investment advice, and it can't see competitive position or risk factors. "
        "Verify the actual results yourself before acting."
    )
    return " ".join(parts)
