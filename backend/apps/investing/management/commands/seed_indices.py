"""
Seeds the Index rows the dashboard's ticker cards render -- run once
after migrating. apps.investing.tasks.sync_index_constituents_and_prices
only syncs indices that already exist as rows here; it doesn't
discover new ones on its own (NSE has no "list all indices" endpoint
this client uses).

Usage:
    python manage.py seed_indices
"""

from django.core.management.base import BaseCommand

from apps.investing.models import Index

# name: NSE/BSE's own index name (what NSEDataClient.
# get_index_constituents_csv derives its CSV filename slug from, for
# NSE ones). symbol: broker-style short ticker for
# display. SENSEX is BSE -- included so the dashboard card exists and
# clearly shows "not synced" rather than being absent entirely (see
# sync_index_constituents_and_prices's own docstring on why it can't
# actually be synced by this client).
DEFAULT_INDICES = [
    ("NIFTY 50", "NIFTY", Index.Exchange.NSE),
    ("NIFTY BANK", "BANKNIFTY", Index.Exchange.NSE),
    ("NIFTY IT", "NIFTYIT", Index.Exchange.NSE),
    ("NIFTY AUTO", "NIFTYAUTO", Index.Exchange.NSE),
    ("NIFTY FMCG", "NIFTYFMCG", Index.Exchange.NSE),
    ("NIFTY PHARMA", "NIFTYPHARMA", Index.Exchange.NSE),
    ("SENSEX", "SENSEX", Index.Exchange.BSE),
]


class Command(BaseCommand):
    help = "Seed the default tracked indices (NIFTY 50, NIFTY BANK, SENSEX, ...)."

    def handle(self, *args, **options):
        created = 0
        for name, symbol, exchange in DEFAULT_INDICES:
            _, was_created = Index.objects.get_or_create(
                name=name, defaults={"symbol": symbol, "exchange": exchange},
            )
            created += int(was_created)
        self.stdout.write(self.style.SUCCESS(
            f"{created} new index row(s) created ({len(DEFAULT_INDICES) - created} already existed)."
        ))
