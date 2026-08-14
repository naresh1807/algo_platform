"""
"Is the price high, low, or medium?" -- the trader asked for this
explicitly, and apps.investing.fundamental_score's own honest-gaps
note (see the project README) already flagged its absence: a great
company at a terrible price still scored well before this module
existed.

Primary method: percentile rank of the CURRENT P/E against the
stock's OWN historical P/E values (from apps.investing.models.
FundamentalSnapshot -- real reported figures, never fabricated). This
is self-referential on purpose -- it doesn't need a sector-average P/E
database this scaffold doesn't have, and "cheap relative to its own
history" is a meaningful, real signal on its own (the classic "buy
good businesses when the market is pessimistic about them" read).

Fallback: when a stock has fewer than MIN_HISTORY_POINTS PE values on
record (a newly-added watchlist stock, most commonly), there's no
history to rank against yet -- falls back to fixed absolute P/E bands
instead. Documented assumption, generic for Indian large/mid-cap
equities, not sector-adjusted: a capital-intensive business (banks,
infra) and an asset-light one (IT services, FMCG) have structurally
different "normal" P/E ranges that this fallback can't tell apart --
the percentile method above doesn't have this problem since it's
compared against the SAME business's own history, which is exactly why
it's the primary method and this is only a fallback.
"""

from __future__ import annotations

MIN_HISTORY_POINTS = 3

# Absolute P/E fallback bands -- generic, not sector-adjusted (see
# module docstring). Only used when there isn't enough of the stock's
# own PE history to rank against yet.
_ABSOLUTE_BANDS = [
    (40, "High"),
    (25, "Medium"),
    (0, "Low"),
]


def _label_from_score(score: float) -> str:
    if score >= 70:
        return "Low"      # cheap relative to its own history
    if score >= 40:
        return "Medium"
    return "High"         # expensive relative to its own history


def assess_valuation(stock) -> dict | None:
    """
    Returns {"label": "Low"|"Medium"|"High", "score": 0-100 (higher =
    cheaper), "current_pe": float, "method": "percentile"|"absolute",
    "detail": str} or None if there's no P/E data at all yet (never a
    fabricated valuation call).
    """
    from .models import FundamentalSnapshot

    # pe_ratio__gte=0 (not just isnull=False) -- a negative P/E (a
    # loss-making quarter) isn't a "cheap" earnings multiple, it's not
    # a usable earnings multiple at all, and falls through every
    # _ABSOLUTE_BANDS threshold below (all >= 0) with no match. Same
    # "None means unknown" convention as everywhere else: treat a
    # negative P/E quarter as no usable data, not as a fabricated label.
    snapshots = list(
        stock.fundamentals.filter(pe_ratio__isnull=False, pe_ratio__gte=0).order_by("-period_end")[:12]
    )
    if not snapshots:
        return None

    current_pe = snapshots[0].pe_ratio
    history = [s.pe_ratio for s in snapshots]

    if len(history) >= MIN_HISTORY_POINTS:
        below_or_equal = sum(1 for pe in history if pe <= current_pe)
        percentile = below_or_equal / len(history) * 100.0
        score = 100.0 - percentile  # lower PE percentile (cheaper) -> higher score
        label = _label_from_score(score)
        detail = (
            f"P/E {current_pe:.1f} sits at the {percentile:.0f}th percentile of its own last "
            f"{len(history)} reported quarters (P/E range {min(history):.1f}-{max(history):.1f})."
        )
        return {"label": label, "score": round(score, 1), "current_pe": current_pe, "method": "percentile", "detail": detail}

    for threshold, label in _ABSOLUTE_BANDS:
        if current_pe >= threshold:
            score = {"High": 15.0, "Medium": 50.0, "Low": 85.0}[label]
            detail = (
                f"P/E {current_pe:.1f} -- not enough of {stock.symbol}'s own history yet "
                f"({len(history)} quarter(s)) to rank against, using generic P/E bands instead."
            )
            return {"label": label, "score": score, "current_pe": current_pe, "method": "absolute", "detail": detail}

    return None  # unreachable given the 0-threshold catch-all above
