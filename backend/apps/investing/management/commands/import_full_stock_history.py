"""
Full multi-year historical OHLC backfill -- "complete previous data
for all stocks, exact like Upstox/Angel One" was the trader's own
request. apps.investing.tasks.refresh_watchlist_price_history only
keeps a rolling ~400-day window current on a weekly schedule; this
command is the actual bulk-backfill tool, run by hand.

Deliberately a management command, NOT a Celery beat schedule entry:
NSE's historical-data endpoint appears to cap how much date range a
single request can cover (commonly reported as roughly a year at a
time, though this client hasn't verified that limit itself -- see
apps.investing.fundamentals_client's own honesty caveat), so a full
5-year backfill for even one stock means several sequential requests,
and doing that automatically for every tracked stock on a schedule
would be a much heavier, more rate-limit-prone load against an
unofficial API than anything else in this codebase's beat schedule
attempts. A deliberate, human-run command keeps that load a conscious
choice, not something that silently happens every week.

Usage:
    python manage.py import_full_stock_history --symbol TCS --years 5
    python manage.py import_full_stock_history --all-tracked --years 5
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

CHUNK_DAYS = 365  # conservative -- see module docstring on NSE's (unverified) per-request range limit
PAUSE_BETWEEN_REQUESTS_SECONDS = 1.0  # be polite to an unofficial, unrate-limited-by-us endpoint


class Command(BaseCommand):
    help = "Backfill multiple years of daily OHLC history for one stock or every currently-tracked stock."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--symbol", help="Backfill a single stock symbol, e.g. TCS.")
        group.add_argument(
            "--all-tracked", action="store_true",
            help="Backfill every stock apps.investing.tasks._tracked_stocks_queryset currently returns "
                 "(watchlisted or an index constituent).",
        )
        parser.add_argument("--years", type=int, default=5, help="How many years back to fetch (default: 5).")

    def handle(self, *args, **options):
        from apps.investing.fundamentals_client import NSEDataClient
        from apps.investing.models import Stock
        from apps.investing.tasks import _tracked_stocks_queryset

        if options["symbol"]:
            symbol = options["symbol"].strip().upper()
            stock, _ = Stock.objects.get_or_create(symbol=symbol, defaults={"name": symbol})
            stocks = [stock]
        else:
            stocks = list(_tracked_stocks_queryset())
            if not stocks:
                raise CommandError(
                    "No tracked stocks yet -- add some to a watchlist, or run sync_index_constituents_and_prices "
                    "(after seed_indices) first, or use --symbol for a specific one."
                )

        client = NSEDataClient()
        years = options["years"]
        # localdate(), not date.today() -- date.today() follows the
        # host machine's own system timezone, which would silently
        # request the wrong day's date-range boundary from NSE if this
        # ever runs somewhere not already set to IST (found and fixed
        # as the same class of bug in apps/market_data/broker_client.py
        # -- see that file's own note on why this matters).
        from django.utils import timezone as django_timezone
        today = django_timezone.localdate()

        total_bars = 0
        for stock in stocks:
            self.stdout.write(f"Backfilling {stock.symbol} ({years} year(s))...")
            bars_for_stock = 0

            # Walk backward in CHUNK_DAYS-sized windows -- NSE's endpoint
            # is called once per window, oldest window last, so a
            # Ctrl-C partway through still leaves the most recent
            # (most useful) history already saved.
            window_end = today
            window_start = max(today - timedelta(days=years * 365), today - timedelta(days=CHUNK_DAYS))

            while window_end > today - timedelta(days=years * 365):
                try:
                    bars_for_stock += self._import_window(client, stock, window_start, window_end)
                except Exception:
                    logger.exception("Backfill window failed for %s (%s to %s)", stock.symbol, window_start, window_end)
                    self.stderr.write(self.style.WARNING(f"  window {window_start}..{window_end} failed, continuing"))

                window_end = window_start - timedelta(days=1)
                window_start = max(window_end - timedelta(days=CHUNK_DAYS), today - timedelta(days=years * 365))
                time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

            self.stdout.write(f"  {stock.symbol}: {bars_for_stock} bar(s) imported/updated.")
            total_bars += bars_for_stock

        self.stdout.write(self.style.SUCCESS(f"Done. {total_bars} total bar(s) across {len(stocks)} stock(s)."))

    def _import_window(self, client, stock, window_start: date, window_end: date) -> int:
        from datetime import datetime

        from apps.investing.models import StockPriceHistory
        from apps.investing.tasks import _to_decimal

        raw = client.get_historical_prices(stock.symbol, window_start, window_end)
        created_or_updated = 0
        for row in raw:
            date_raw = row.get("CH_TIMESTAMP") or row.get("date")
            if not date_raw:
                continue
            try:
                bar_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            StockPriceHistory.objects.update_or_create(
                stock=stock, date=bar_date,
                defaults={
                    "open": _to_decimal(row.get("CH_OPENING_PRICE") or row.get("open")),
                    "high": _to_decimal(row.get("CH_TRADE_HIGH_PRICE") or row.get("high")),
                    "low": _to_decimal(row.get("CH_TRADE_LOW_PRICE") or row.get("low")),
                    "close": _to_decimal(row.get("CH_CLOSING_PRICE") or row.get("close")),
                    "volume": row.get("CH_TOT_TRADED_QTY") or row.get("volume"),
                    "source": "nse_historical_backfill",
                },
            )
            created_or_updated += 1
        return created_or_updated
