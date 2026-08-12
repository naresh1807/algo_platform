"""
Populates OptionContract rows for one underlying+expiry from Angel
One's real instrument master (apps/options/instrument_master.py via
OptionChainClient.fetch_contract_list). For routine use,
apps.options.tasks.sync_watchlist_option_contracts now does this
automatically every week for settings.WATCHLIST's nearest expiry --
this command remains for syncing a SPECIFIC expiry on demand (e.g. a
further-out expiry the weekly task hasn't reached yet, or a one-off
manual refresh).

Usage:
    python manage.py sync_option_contracts --underlying NIFTY --expiry 2026-08-28
    python manage.py sync_option_contracts --underlying NIFTY --list-expiries
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Discover real option contracts (strike/type/token) for an underlying+expiry from Angel One and upsert OptionContract rows."

    def add_arguments(self, parser):
        parser.add_argument("--underlying", default="NIFTY", help="e.g. NIFTY, BANKNIFTY")
        parser.add_argument("--expiry", help="YYYY-MM-DD. Required unless --list-expiries is passed.")
        parser.add_argument(
            "--list-expiries", action="store_true",
            help="Instead of syncing, just print the expiries currently listed for --underlying and exit.",
        )

    def handle(self, *args, **options):
        underlying = options["underlying"]

        from apps.options.instrument_master import list_expiries

        if options["list_expiries"]:
            expiries = list_expiries(underlying)
            if not expiries:
                self.stdout.write(self.style.WARNING(f"No listed expiries found for {underlying}."))
                return
            self.stdout.write(f"Expiries currently listed for {underlying}:")
            for e in expiries:
                self.stdout.write(f"  {e.isoformat()}")
            return

        if not options["expiry"]:
            raise CommandError("--expiry YYYY-MM-DD is required (or pass --list-expiries to see valid values).")
        try:
            expiry = date.fromisoformat(options["expiry"])
        except ValueError:
            raise CommandError(f"--expiry {options['expiry']!r} is not a valid YYYY-MM-DD date.")

        from apps.options.broker_client import OptionChainClient
        from apps.options.models import OptionContract

        client = OptionChainClient()
        self.stdout.write(f"Fetching contract list for {underlying} {expiry}...")
        contracts = client.fetch_contract_list(underlying, expiry)

        if not contracts:
            self.stdout.write(self.style.WARNING(
                f"No contracts found for {underlying} {expiry}. Run with --list-expiries "
                f"to see which expiries actually exist for {underlying} right now."
            ))
            return

        created = 0
        updated = 0
        for c in contracts:
            obj, was_created = OptionContract.objects.update_or_create(
                underlying=underlying, expiry=expiry,
                strike=c["strike"], option_type=c["option_type"],
                defaults={"symbol_token": c["symbol_token"]},
            )
            created += int(was_created)
            updated += int(not was_created)

        self.stdout.write(self.style.SUCCESS(
            f"Done. {len(contracts)} contracts for {underlying} {expiry}: "
            f"{created} created, {updated} already existed (token refreshed). "
            f"Run 'python manage.py shell -c \"from apps.options.tasks import "
            f"ingest_option_chain_snapshots; ingest_option_chain_snapshots()\"' "
            f"(or wait for the Celery beat schedule) to pull the first real quotes."
        ))
