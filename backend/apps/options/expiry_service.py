"""
The single, shared expiry-resolution service -- every caller that needs
to know "which expiry counts as current/next/eligible right now" (the
sync_option_contracts management command, apps.options.tasks' Celery
jobs, apps.options.signals_engine's nearest_expiry/select_expiry,
apps.options.views' expiry/chain endpoints, and the frontend via those
endpoints) goes through this module instead of re-deriving its own
date filter. That duplication is exactly what caused the platform to
keep offering an already-expired contract: apps.options.signals_engine.
nearest_expiry(), select_expiry(), and apps.options.views.
OptionExpiriesView each had their OWN `expiry__gte=timezone.localdate()`
filter -- a pure DATE comparison with no time-of-day awareness, so an
expiry stayed "eligible" all the way through midnight on its own expiry
day, hours after the market (and that contract's own tradeable life)
had actually closed.

Timezone-aware, Asia/Kolkata throughout (settings.TIME_ZONE), and every
function accepts an explicit `at: datetime | None` the same way
apps.market_data.market_hours.is_market_open does -- lets every
scenario (expiry day before/after cutoff, weekend, month/year boundary,
holiday-shifted expiry from the instrument master) be unit-tested
against a fixed instant without depending on when the test actually
runs, no time-freezing library needed (none is installed in this
project, and this matches the established convention exactly).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from django.conf import settings
from django.utils import timezone

from .models import OptionContract, OptionSyncStatus


def _ist_now(at: datetime | None = None) -> datetime:
    moment = at or timezone.now()
    return timezone.localtime(moment, timezone.get_default_timezone())


def is_expiry_eligible(expiry: date, at: datetime | None = None) -> bool:
    """
    The one cutoff rule, used everywhere:
      - expiry > today            -> always eligible (a real future date)
      - expiry < today            -> never eligible (already fully past)
      - expiry == today           -> eligible only strictly BEFORE
                                      settings.OPTIONS_EXPIRY_CUTOFF_TIME;
                                      at/after the cutoff it rolls over.
    Never assumes a fixed weekday or a fixed calendar cadence -- `expiry`
    always comes from real OptionContract rows (themselves sourced from
    Angel One's instrument master, see apps.options.instrument_master),
    so an exchange-holiday-shifted expiry is handled automatically: this
    function only ever compares the ACTUAL date it's given against
    today, never recomputes what the expiry "should" be.
    """
    now = _ist_now(at)
    today = now.date()
    if expiry > today:
        return True
    if expiry < today:
        return False
    return now.time() < settings.OPTIONS_EXPIRY_CUTOFF_TIME


def list_eligible_expiries(underlying: str, at: datetime | None = None, limit: int | None = None) -> list[date]:
    """
    Every distinct expiry with synced OptionContract rows for
    `underlying` that passes is_expiry_eligible, nearest first. This is
    what "available_expiries" (API/frontend) and every mode in
    resolve_current_expiry below are built from -- always real, already-
    synced data, never a guess at what Angel One "should" be listing.
    """
    candidates = (
        OptionContract.objects.filter(underlying=underlying)
        .order_by("expiry").values_list("expiry", flat=True).distinct()
    )
    eligible = [e for e in candidates if is_expiry_eligible(e, at=at)]
    return eligible[:limit] if limit else eligible


def resolve_current_expiry(underlying: str, mode: str | None = None, at: datetime | None = None) -> date | None:
    """
    The cutoff-aware replacement for the old `expiry__gte=today` filters
    in apps.options.signals_engine.nearest_expiry/select_expiry and
    apps.options.views.OptionExpiriesView. Same mode vocabulary
    select_expiry already established (current_week/next_week/monthly/
    custom via apps.options.models.OptionsStrategySetting, or None for
    the plain "nearest eligible" behavior nearest_expiry() wants) --
    kept here so BOTH callers share one cutoff rule instead of two
    copies that could drift.
    """
    eligible = list_eligible_expiries(underlying, at=at)
    if not eligible:
        return None

    if mode is None:
        return eligible[0]

    mode = mode.lower()
    if mode == "current_week":
        return eligible[0]
    if mode == "next_week":
        return eligible[1] if len(eligible) > 1 else None
    if mode == "monthly":
        from .instrument_master import list_expiries

        listed = list_expiries(underlying, limit=12)
        if not listed:
            return None
        earliest_month = (listed[0].year, listed[0].month)
        in_month = [e for e in listed if (e.year, e.month) == earliest_month]
        monthly_expiry = max(in_month)
        return monthly_expiry if monthly_expiry in eligible else None
    if mode == "custom":
        from .models import get_options_strategy_settings

        custom = get_options_strategy_settings().custom_expiry
        if custom is None or custom not in eligible:
            return None
        return custom
    return None


def next_expiry_after(underlying: str, at: datetime | None = None) -> date | None:
    """The SECOND nearest eligible expiry -- what rolls into "current" the moment today's does."""
    eligible = list_eligible_expiries(underlying, at=at)
    return eligible[1] if len(eligible) > 1 else None


def rollover_required(underlying: str, at: datetime | None = None) -> bool:
    """
    DB-ONLY check (no Angel One call) -- this is deliberately the entire
    contract of apps.options.tasks.options_sync_health_check: true when
    there is no eligible current expiry at all, or fewer than
    settings.OPTIONS_EXPIRY_SYNC_COUNT eligible expiries remain synced
    (the buffer is thinning and a resync should top it back up before it
    hits zero, not after). A periodic health check built on this never
    itself calls the broker -- it only decides whether apps.options.
    contract_sync.sync_underlying_contracts (which DOES call the
    broker/instrument-master) needs to run.
    """
    eligible = list_eligible_expiries(underlying, at=at)
    return len(eligible) < settings.OPTIONS_EXPIRY_SYNC_COUNT


@dataclass
class ExpiryStatus:
    underlying: str
    current_expiry: date | None
    next_expiry: date | None
    available_expiries: list[date]
    last_successful_sync: datetime | None
    rollover_required: bool

    def as_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "current_expiry": self.current_expiry.isoformat() if self.current_expiry else None,
            "next_expiry": self.next_expiry.isoformat() if self.next_expiry else None,
            "available_expiries": [e.isoformat() for e in self.available_expiries],
            "last_successful_sync": self.last_successful_sync.isoformat() if self.last_successful_sync else None,
            "rollover_required": self.rollover_required,
        }


def expiry_status(underlying: str, at: datetime | None = None) -> ExpiryStatus:
    """
    The single call apps.options.views.OptionExpiryStatusView (and
    anything else that wants the full picture) needs -- current/next/
    available expiries plus sync health, assembled from this module's
    own functions above and OptionSyncStatus, so the API layer contains
    no expiry-selection logic of its own.
    """
    eligible = list_eligible_expiries(underlying, at=at)
    sync_row = OptionSyncStatus.objects.filter(underlying=underlying).first()
    return ExpiryStatus(
        underlying=underlying,
        current_expiry=eligible[0] if eligible else None,
        next_expiry=eligible[1] if len(eligible) > 1 else None,
        available_expiries=eligible,
        last_successful_sync=sync_row.last_successful_sync if sync_row else None,
        rollover_required=len(eligible) < settings.OPTIONS_EXPIRY_SYNC_COUNT,
    )


def validate_requested_expiry(underlying: str, requested: date, at: datetime | None = None) -> tuple[date | None, bool]:
    """
    For any endpoint that accepts a frontend-supplied `expiry` query
    param (apps.options.views.OptionChainView, OptionsAnalyticsView,
    BestStrikeView): returns (expiry_to_use, was_substituted).
    `requested` is honored as-is if it's still eligible; otherwise falls
    back to resolve_current_expiry() (None if nothing eligible exists at
    all) -- this is what stops those endpoints from silently serving an
    expired chain just because the caller asked for a date that used to
    be valid.
    """
    if is_expiry_eligible(requested, at=at) and OptionContract.objects.filter(underlying=underlying, expiry=requested).exists():
        return requested, False
    return resolve_current_expiry(underlying, at=at), True
