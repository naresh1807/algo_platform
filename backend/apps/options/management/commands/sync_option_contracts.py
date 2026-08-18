"""
Populates OptionContract rows from Angel One's real instrument master
(apps/options/instrument_master.py) via apps.options.contract_sync --
the SAME idempotent, lock-protected sync routine apps.options.tasks'
Celery jobs use, so a manual run here and the automated daily/rollover
schedule (config/celery.py) can never disagree about how a sync
actually happens.

Usage:
    python manage.py sync_option_contracts --underlying NIFTY
    python manage.py sync_option_contracts --all
    python manage.py sync_option_contracts --underlying NIFTY --expiry 2026-08-28
    python manage.py sync_option_contracts --underlying NIFTY --list-expiries
    python manage.py sync_option_contracts --all --force-refresh --expiry-count 4
    python manage.py sync_option_contracts --all --dry-run
"""

import sys
from datetime import date

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Sync real option contracts (strike/type/token) from Angel One's instrument master into OptionContract rows."

    def add_arguments(self, parser):
        parser.add_argument("--underlying", help="e.g. NIFTY, BANKNIFTY. Ignored if --all is passed.")
        parser.add_argument(
            "--all", action="store_true",
            help="Sync every underlying in settings.OPTIONS_PIPELINE_UNDERLYINGS instead of just one.",
        )
        parser.add_argument(
            "--expiry", help="YYYY-MM-DD. Sync ONLY this one specific expiry (legacy single-expiry mode) "
                              "instead of the usual nearest --expiry-count window.",
        )
        parser.add_argument(
            "--list-expiries", action="store_true",
            help="Instead of syncing, just print the expiries currently listed for --underlying and exit.",
        )
        parser.add_argument(
            "--expiry-count", type=int, default=None,
            help="How many upcoming expiries to sync (default: settings.OPTIONS_EXPIRY_SYNC_COUNT).",
        )
        parser.add_argument(
            "--force-refresh", action="store_true",
            help="Force a fresh instrument-master download instead of using the cached copy.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Fetch and report what would change without writing anything to the database.",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        if options["list_expiries"]:
            from apps.options.instrument_master import InstrumentMasterError, list_expiries

            underlying = options["underlying"] or "NIFTY"
            try:
                expiries = list_expiries(underlying)
            except InstrumentMasterError as exc:
                raise CommandError(f"Could not list expiries for {underlying}: {exc}")
            if not expiries:
                self.stdout.write(self.style.WARNING(f"No listed expiries found for {underlying}."))
                return
            self.stdout.write(f"Expiries currently listed for {underlying}:")
            for e in expiries:
                self.stdout.write(f"  {e.isoformat()}")
            return

        from apps.options.contract_sync import sync_all_underlyings, sync_underlying_contracts
        from apps.options.instrument_master import InstrumentMasterError

        # Legacy single-expiry mode: --underlying + --expiry together
        # still means "sync exactly this one expiry," preserved for
        # backward compatibility (a further-out expiry the regular
        # window hasn't reached yet, or a one-off targeted refresh).
        if options["expiry"]:
            if options["all"]:
                raise CommandError("--expiry cannot be combined with --all (pick one underlying).")
            underlying = options["underlying"] or "NIFTY"
            try:
                expiry = date.fromisoformat(options["expiry"])
            except ValueError:
                raise CommandError(f"--expiry {options['expiry']!r} is not a valid YYYY-MM-DD date.")
            self.stdout.write(f"Fetching contract list for {underlying} {expiry}...")
            try:
                from apps.options.broker_client import get_option_chain_client
                from apps.options.expiry_service import is_expiry_eligible
                from apps.options.models import OptionContract
                from django.db import transaction

                client = get_option_chain_client()
                contracts = client.fetch_contract_list(underlying, expiry)
                if not contracts:
                    self.stdout.write(self.style.WARNING(
                        f"No contracts found for {underlying} {expiry}. Run with --list-expiries "
                        f"to see which expiries actually exist for {underlying} right now."
                    ))
                    sys.exit(1)

                created = updated = 0
                with transaction.atomic():
                    for c in contracts:
                        _, was_created = OptionContract.objects.update_or_create(
                            underlying=underlying, expiry=expiry,
                            strike=c["strike"], option_type=c["option_type"],
                            defaults={
                                "symbol_token": c["symbol_token"],
                                "tradingsymbol": c["tradingsymbol"],
                                "lot_size": c["lot_size"],
                                "is_active": is_expiry_eligible(expiry),
                            },
                        )
                        created += int(was_created)
                        updated += int(not was_created)
                    if options["dry_run"]:
                        transaction.set_rollback(True)
            except InstrumentMasterError as exc:
                raise CommandError(f"Instrument master error: {exc}")

            self.stdout.write(self.style.SUCCESS(
                f"{'[DRY RUN] Would sync' if options['dry_run'] else 'Done.'} {len(contracts)} contracts for "
                f"{underlying} {expiry}: {created} inserted, {updated} updated."
            ))
            return

        underlyings = None
        if options["all"]:
            underlyings = settings.OPTIONS_PIPELINE_UNDERLYINGS
        elif options["underlying"]:
            underlyings = [options["underlying"]]
        else:
            raise CommandError("Pass --underlying NAME, --all, or --list-expiries.")

        results = sync_all_underlyings(
            underlyings=underlyings, expiry_count=options["expiry_count"],
            force_refresh=options["force_refresh"], dry_run=options["dry_run"],
        )

        had_failure = False
        for underlying, result in results.items():
            prefix = "[DRY RUN] " if result.dry_run else ""
            if not result.ok:
                had_failure = True
                self.stdout.write(self.style.ERROR(
                    f"{prefix}{underlying}: FAILED -- {result.error or result.skipped_reason}"
                ))
                continue
            if result.skipped_reason:
                self.stdout.write(self.style.WARNING(f"{prefix}{underlying}: skipped -- {result.skipped_reason}"))
                continue
            self.stdout.write(self.style.SUCCESS(
                f"{prefix}{underlying}: current_expiry={result.current_expiry or '—'} "
                f"next_expiry={result.next_expiry or '—'} "
                f"expiries_synced={len(result.expiries_synced)} "
                f"inserted={result.inserted} updated={result.updated} "
                f"deactivated={result.deactivated} invalid_skipped={result.invalid_skipped}"
            ))

        if had_failure:
            sys.exit(1)
