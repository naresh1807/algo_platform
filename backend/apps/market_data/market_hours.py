"""
NSE market-calendar helper: "is the market actually open right now."
This is the module config/celery.py's own beat_schedule comments have
been flagging as missing since generate-signals-every-5-minutes was
first scheduled ("restrict to NSE market hours... once a market-
calendar helper exists").

Three checks, all required for the market to be open:
  1. Weekday (Mon-Fri) -- NSE's equity segment doesn't trade weekends.
  2. Time-of-day (09:15-15:30 IST) -- NSE's regular equity/index session.
  3. Not an NSE trading holiday.

HONESTY NOTE on holidays, same spirit as this codebase's other external
-data caveats (apps.investing.fundamentals_client's NSE-scraping note,
apps.news.rss_client's "feeds can change" note): NSE publishes an
official holiday circular every December for the following calendar
year, and most of those dates are festival-calendar-based (Holi, Eid,
Diwali, Dussehra, Guru Nanak Jayanti, ...) that move every year and
cannot be reliably computed or guessed -- only looked up from that
official circular. NSE_HOLIDAYS below only includes the small set of
FIXED-DATE national holidays (same calendar date every year) that can
be stated with confidence; movable festival holidays for the current
year must be added by hand from NSE's actual published circular
(nseindia.com publishes it, though see that module's own note on why
this codebase can't currently fetch it automatically -- Akamai bot
protection blocks scripted requests to nseindia.com entirely). Until
those are added, is_market_open() will return True (market treated as
open) on an actual movable-date holiday -- a false positive, not a
false negative, so nothing gets silently blocked that shouldn't be;
the cost of the gap is a handful of harmless no-op signal-generation
cycles on unlisted holidays, not a missed trading day.
"""

from __future__ import annotations

from datetime import date, datetime, time

from django.utils import timezone as django_timezone

MARKET_OPEN_TIME = time(9, 15)
MARKET_CLOSE_TIME = time(15, 30)

# Fixed-date national holidays only -- see module docstring. Add
# movable festival-calendar holidays for the current year here once
# confirmed against NSE's official circular (typically published each
# December for the following year); format is a plain set of
# datetime.date, so any real date can be added directly regardless of
# how it was determined.
NSE_HOLIDAYS: set[date] = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
}


def is_market_open(at: datetime | None = None) -> tuple[bool, str]:
    """
    Returns (is_open, reason) -- same "boolean plus explainable reason"
    convention apps.risk.engine's pre-trade checks use throughout, so
    callers can log/return WHY the market is considered closed, not
    just that it is.

    `at` defaults to django_timezone.now() (converted to IST below) --
    accepting an explicit datetime is what lets this be unit-tested
    against fixed timestamps (a weekend, a holiday, 9:00am, etc.)
    without depending on when the test actually runs.
    """
    moment = at or django_timezone.now()
    ist_moment = django_timezone.localtime(moment, django_timezone.get_default_timezone())
    local_date = ist_moment.date()
    local_time = ist_moment.time()

    if ist_moment.weekday() >= 5:  # Saturday=5, Sunday=6
        return False, f"{local_date.isoformat()} is a weekend -- NSE is closed."

    if local_date in NSE_HOLIDAYS:
        return False, f"{local_date.isoformat()} is an NSE trading holiday."

    if local_time < MARKET_OPEN_TIME or local_time >= MARKET_CLOSE_TIME:
        return False, (
            f"{local_time.strftime('%H:%M')} IST is outside NSE's regular session "
            f"({MARKET_OPEN_TIME.strftime('%H:%M')}-{MARKET_CLOSE_TIME.strftime('%H:%M')} IST)."
        )

    return True, ""
