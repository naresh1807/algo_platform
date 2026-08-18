"""
Event Risk Engine -- EXPLICITLY UNAVAILABLE (with one honest partial
exception: options expiry itself, which this platform DOES track).

No economic/central-bank/inflation/earnings calendar data source
exists anywhere in this codebase (confirmed: no calendar API client,
no event table, nothing resembling one). apps.market_data.market_hours
already tracks NSE trading holidays (a real, if manually-maintained,
calendar) and apps.risk.engine._is_options_expiry_day already tracks
options-expiry-day size reduction from real apps.options.OptionContract
data -- neither of those is duplicated here.

detect_event_risk() below is the one typed extension point a caller
can check before trusting a signal's confidence/size -- it currently
can only ever report the options-expiry case (reusing apps.risk.engine's
own real check) and otherwise returns "unavailable" for genuine
macro/earnings event risk, never a fabricated "no event today" all-
clear that would imply more was actually checked than was.
"""

from __future__ import annotations


def detect_event_risk(symbol: str) -> dict:
    """
    Returns {"available": bool, "is_options_expiry_day": bool | None,
    "macro_event_risk": "unavailable", "reason": str}. The options-
    expiry flag is REAL (reuses apps.risk.engine's own existing check,
    lazy-imported the same way that module already lazy-imports across
    apps to avoid a load-time dependency cycle); macro/earnings/policy
    event risk is honestly unavailable -- there is nothing to check it
    against.
    """
    from apps.risk.engine import _is_options_expiry_day

    is_expiry_day = _is_options_expiry_day(symbol)
    return {
        "available": True,
        "is_options_expiry_day": is_expiry_day,
        "macro_event_risk": "unavailable",
        "reason": (
            "No economic/central-bank/earnings calendar data source exists in this platform -- "
            "macro event risk cannot be checked. Options-expiry-day status IS real (reuses apps."
            "risk.engine's own tracked OptionContract expiry data)."
        ),
    }
