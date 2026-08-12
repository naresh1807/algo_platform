"""
manual 13.10 "Decision Confidence": maps the ML win-probability (a 0-1
model output apps.learning.ml_predict already produces) onto the
manual's own named 0-100 bands.

Purely a labeling/reporting layer -- it never changes what
apps.signals.engine or apps.risk.engine decide; those already act on
the raw probability directly via
apps.learning.ml_predict.should_reject_for_low_confidence /
ml_confidence_size_multiplier. This exists so JARVIS, the dashboard,
and TradeReview/DailyReviewNote can report a confidence band in the
manual's own vocabulary instead of a bare float.
"""

from __future__ import annotations


def confidence_band(win_probability: float | None) -> str:
    if win_probability is None:
        return "Unrated"  # no trained model yet -- honest, not a fabricated band
    pct = win_probability * 100
    if pct >= 95:
        return "Very Strong"
    if pct >= 80:
        return "Strong"
    if pct >= 60:
        return "Average"
    return "No Recommendation"
