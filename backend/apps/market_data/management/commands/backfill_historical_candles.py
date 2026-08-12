"""
One-off backfill: pulls real candles from Angel One (via BrokerClient,
apps/market_data/broker_client.py) for a wider date range than the
recurring ingest_watchlist_candles task uses, and upserts them into
HistoricalData.

Why this exists separately from ingest_watchlist_candles (apps/market_data/tasks.py):
that task is designed to run every 5 minutes via Celery beat with a
small lookback_days=5 -- correct for "keep the last little while
topped up", wrong for "I just switched BROKER_MODE=live and the chart
is empty, give me real history right now." This command is for the
second situation: run it once (or whenever you want a fresh pull),
get real OHLCV rows immediately, no Celery worker/beat required.

Requires BROKER_MODE=live and real ANGEL_ONE_* credentials in .env --
this will raise BrokerAuthError immediately if either is missing,
same as the recurring task would.

Usage:
    python manage.py backfill_historical_candles
    python manage.py backfill_historical_candles --symbol NIFTY --timeframe 5m --days 30
    python manage.py backfill_historical_candles --symbol BANKNIFTY --timeframe 1d --days 365
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Backfill real OHLCV candles from Angel One for one symbol/timeframe (one-off, not the recurring 5m job)."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", default="NIFTY", help="Must exist in broker_client.SYMBOL_TOKENS.")
        parser.add_argument(
            "--timeframe", default="5m",
            choices=["1m", "3m", "5m", "10m", "15m", "30m", "1h", "1d"],
        )
        parser.add_argument(
            "--days", type=int, default=30,
            help="How many days of history to pull. Angel One's historical API caps how far back "
                 "1m/5m data goes (roughly 30/90 days respectively) -- for 1d candles you can go back further.",
        )

    def handle(self, *args, **options):
        if settings.BROKER_MODE != "live":
            raise CommandError(
                f"BROKER_MODE={settings.BROKER_MODE!r}, not 'live'. Set BROKER_MODE=live and fill in "
                f"ANGEL_ONE_API_KEY/CLIENT_ID/PASSWORD/TOTP_SECRET in .env first, then re-run this."
            )

        from apps.market_data.broker_client import BrokerAuthError, BrokerClient
        from apps.market_data.models import HistoricalData

        symbol = options["symbol"]
        timeframe = options["timeframe"]
        days = options["days"]

        client = BrokerClient()
        self.stdout.write(f"Fetching {days}d of real {symbol} {timeframe} candles from Angel One...")

        try:
            candles = client.fetch_recent_candles(symbol, timeframe, lookback_days=days)
        except BrokerAuthError as exc:
            raise CommandError(f"Angel One login failed: {exc}")
        except Exception as exc:
            raise CommandError(f"Candle fetch failed: {exc}")

        if not candles:
            self.stdout.write(self.style.WARNING(
                "Angel One returned zero candles. Common causes: symbol/timeframe combo has no data in "
                "this range, or the requested range exceeds what Angel One's API allows for this interval."
            ))
            return

        with transaction.atomic():
            created = HistoricalData.objects.bulk_create(
                [HistoricalData(**c) for c in candles], ignore_conflicts=True,
            )

        self.stdout.write(self.style.SUCCESS(
            f"Done. Angel One returned {len(candles)} candles, {len(created)} newly inserted "
            f"(rest were already in HistoricalData, or duplicates within this pull). "
            f"Refresh the dashboard -- the chart should now show real {symbol} {timeframe} data."
        ))
