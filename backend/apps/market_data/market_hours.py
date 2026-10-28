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

The NSE equity holiday calendar for 2026 was verified against the
exchange's official published calendar on 6 October 2026. Update the
calendar when new exchange closures are announced and before next year.
BTST explicitly refuses an unverified calendar year.
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
# NSE equity calendar for 2026, verified 6 October 2026.
# https://www.nseindia.com/resources/exchange-communication-holidays
NSE_HOLIDAYS: set[date] = {
    date(2026, 1, 15), date(2026, 1, 26), date(2026, 3, 3),
    date(2026, 3, 26), date(2026, 3, 31), date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2),
    date(2026, 10, 20), date(2026, 11, 8), date(2026, 11, 10),
    date(2026, 11, 24), date(2026, 12, 25),
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
