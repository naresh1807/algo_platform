"""
The one idempotent contract-sync routine -- used by BOTH the
`sync_option_contracts` management command and apps.options.tasks'
Celery jobs, so there is exactly one place that upserts OptionContract
rows from Angel One's instrument master instead of two copies that
could drift (the exact duplication this platform's expired-contract
bug came from: apps.options.tasks.sync_watchlist_option_contracts and
the management command each had their own near-identical upsert loop).

Every sync of one underlying is wrapped in its own transaction and its
own Redis lock (apps.options.sync_lock) -- one underlying's failure or
a concurrent run for the SAME underlying can never corrupt another
underlying's already-committed data, and Celery Beat / a manual command
/ multiple workers can never race on the same underlying at once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from django.db import transaction
from django.db.utils import IntegrityError
from django.utils import timezone

from .expiry_service import is_expiry_eligible
from .instrument_master import InstrumentMasterError
from .models import OptionContract, OptionSyncStatus
from .sync_lock import sync_lock

logger = logging.getLogger(__name__)

SYNC_LOCK_KEY_PREFIX = "options:contract_sync"


@dataclass
class SyncResult:
    underlying: str
    ok: bool
    dry_run: bool = False
    skipped_reason: str | None = None
    error: str | None = None
    expiries_synced: list[str] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    deactivated: int = 0
    invalid_skipped: int = 0
    current_expiry: str | None = None
    next_expiry: str | None = None

    def as_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "expiries_synced": self.expiries_synced,
            "inserted": self.inserted,
            "updated": self.updated,
            "deactivated": self.deactivated,
            "invalid_skipped": self.invalid_skipped,
            "current_expiry": self.current_expiry,
            "next_expiry": self.next_expiry,
        }


def _record_status(underlying: str, *, success: bool, error: str = "", result: dict | None = None) -> None:
    now = timezone.now()
    defaults = {"last_attempt_at": now}
    if success:
        defaults["last_successful_sync"] = now
        defaults["last_error"] = ""
    else:
        defaults["last_error"] = error
    if result is not None:
        defaults["last_result"] = result
    OptionSyncStatus.objects.update_or_create(underlying=underlying, defaults=defaults)


def sync_underlying_contracts(
    underlying: str, expiry_count: int | None = None, force_refresh: bool = False,
    dry_run: bool = False, at: datetime | None = None,
) -> SyncResult:
    """
    Syncs the nearest `expiry_count` (default settings.OPTIONS_EXPIRY_SYNC_COUNT)
    listed expiries for `underlying` from the real instrument master:
    upserts every CE/PE contract Angel One lists for each expiry
    (update_or_create keyed on the existing (underlying, expiry, strike,
    option_type) unique_together -- unchanged from this platform's
    established schema), then recomputes is_active for every OptionContract
    row of this underlying via apps.options.expiry_service.is_expiry_eligible
    so a contract that just rolled past the cutoff is marked inactive
    even if this run didn't re-touch it.

    Never deletes a row -- expired contracts stay (is_active=False) so
    their OptionChainSnapshot history remains attached for backtesting.
    dry_run=True runs the full instrument-master fetch and reports what
    WOULD change without writing anything (wraps the whole body in a
    transaction that's always rolled back).

    Locked per-underlying (apps.options.sync_lock) so Celery Beat, a
    manual command run, and another worker can never sync the same
    underlying concurrently; skips (not blocks) if already locked.
    """
    from django.conf import settings

    expiry_count = expiry_count or settings.OPTIONS_EXPIRY_SYNC_COUNT
    lock_key = f"{SYNC_LOCK_KEY_PREFIX}:{underlying}"

    with sync_lock(lock_key) as acquired:
        if not acquired:
            return SyncResult(underlying=underlying, ok=False, skipped_reason="sync_already_in_progress")

        try:
            with transaction.atomic():
                result = _do_sync(underlying, expiry_count, force_refresh, at=at)
                if dry_run:
                    result.dry_run = True
                    transaction.set_rollback(True)
        except InstrumentMasterError as exc:
            logger.exception("sync_underlying_contracts(%s): instrument master error", underlying)
            _record_status(underlying, success=False, error=str(exc))
            return SyncResult(underlying=underlying, ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 -- any other failure must still leave a clean, recorded state
            logger.exception("sync_underlying_contracts(%s): unexpected failure", underlying)
            _record_status(underlying, success=False, error=str(exc))
            return SyncResult(underlying=underlying, ok=False, error=str(exc))

        if not dry_run:
            _record_status(underlying, success=True, result=result.as_dict())
        return result


def _do_sync(underlying: str, expiry_count: int, force_refresh: bool, at: datetime | None) -> SyncResult:
    from .broker_client import get_option_chain_client
    from .instrument_master import list_expiries

    client = get_option_chain_client()
    expiries = list_expiries(underlying, limit=expiry_count)
    # force_refresh only needs to bust the instrument-master cache once;
    # list_expiries/options_for_expiry both call get_instrument_master()
    # internally, so busting it before the first call is enough to make
    # every subsequent call in this sync this same, freshly-downloaded copy.
    if force_refresh:
        from .instrument_master import get_instrument_master

        get_instrument_master(force_refresh=True)
        expiries = list_expiries(underlying, limit=expiry_count)

    if not expiries:
        return SyncResult(underlying=underlying, ok=True, skipped_reason="no_expiries_listed")

    result = SyncResult(underlying=underlying, ok=True)
    for expiry in expiries:
        contracts = client.fetch_contract_list(underlying, expiry)
        result.expiries_synced.append(expiry.isoformat())
        for c in contracts:
            try:
                # Nested atomic() -> a real SAVEPOINT, not just a
                # try/except -- an IntegrityError (e.g. Angel One
                # reissuing a symbol_token that collides with a
                # different strike/type than our unique_together key
                # expects) leaves Django's outer transaction unusable
                # for further queries unless the failure is contained to
                # its own savepoint that can be rolled back on its own,
                # letting the loop continue and the rest of this
                # underlying's contracts still commit together.
                with transaction.atomic():
                    _, was_created = OptionContract.objects.update_or_create(
                        underlying=underlying, expiry=expiry,
                        strike=c["strike"], option_type=c["option_type"],
                        defaults={
                            "symbol_token": c["symbol_token"],
                            "tradingsymbol": c["tradingsymbol"],
                            "lot_size": c["lot_size"],
                            "is_active": is_expiry_eligible(expiry, at=at),
                        },
                    )
                result.inserted += int(was_created)
                result.updated += int(not was_created)
            except (IntegrityError, KeyError, TypeError, ValueError):
                # A symbol_token collision (Angel One reissuing a token
                # for a different strike/type than what our unique_together
                # key expects) or a malformed row from the instrument
                # master -- counted and skipped, never allowed to abort
                # the whole underlying's sync over one bad record.
                logger.warning(
                    "sync_underlying_contracts(%s): skipping invalid contract record %r", underlying, c,
                )
                result.invalid_skipped += 1

    # Recompute is_active for EVERY row of this underlying, not just the
    # ones just touched above -- an older row from a previously-synced
    # expiry that has now crossed the cutoff must flip to inactive even
    # if this run's `expiries` window no longer includes it.
    all_ids_and_expiry = list(
        OptionContract.objects.filter(underlying=underlying).values_list("id", "expiry", "is_active")
    )
    now_active_ids = [pk for pk, expiry, was_active in all_ids_and_expiry if is_expiry_eligible(expiry, at=at) and not was_active]
    now_inactive_ids = [pk for pk, expiry, was_active in all_ids_and_expiry if not is_expiry_eligible(expiry, at=at) and was_active]
    if now_active_ids:
        OptionContract.objects.filter(id__in=now_active_ids).update(is_active=True)
    if now_inactive_ids:
        OptionContract.objects.filter(id__in=now_inactive_ids).update(is_active=False)
    result.deactivated = len(now_inactive_ids)

    eligible_sorted = sorted(e for e in expiries if is_expiry_eligible(e, at=at))
    result.current_expiry = eligible_sorted[0].isoformat() if eligible_sorted else None
    result.next_expiry = eligible_sorted[1].isoformat() if len(eligible_sorted) > 1 else None
    return result


def sync_all_underlyings(
    underlyings: list[str] | None = None, expiry_count: int | None = None,
    force_refresh: bool = False, dry_run: bool = False, at: datetime | None = None,
) -> dict[str, SyncResult]:
    """Loops sync_underlying_contracts over `underlyings` (default settings.OPTIONS_PIPELINE_UNDERLYINGS) -- one underlying's failure never stops the rest."""
    from django.conf import settings

    underlyings = underlyings or settings.OPTIONS_PIPELINE_UNDERLYINGS
    results = {}
    for underlying in underlyings:
        underlying = underlying.strip()
        results[underlying] = sync_underlying_contracts(
            underlying, expiry_count=expiry_count, force_refresh=force_refresh, dry_run=dry_run, at=at,
        )
    return results
