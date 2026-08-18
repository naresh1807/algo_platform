"""
One-off backfill: pulls real candles from Angel One (via BrokerClient,
apps/market_data/broker_client.py) for a wider date range than the
recurring ingest_watchlist_candles/ingest_index_chart_candles tasks
use, and upserts them into HistoricalData.

Why this exists separately from those recurring tasks: they're
designed to run every few minutes with a small lookback_days -- correct
for "keep the last little while topped up", wrong for "the chart is
empty/thin, give me real history right now." This command is for the
second situation.

CHUNKED to cover more than one request's worth of history: Angel One
caps how far back a SINGLE request can go, per interval (see
broker_client.MAX_LOOKBACK_DAYS -- e.g. 30 days for 1m candles, 2000
for 1d), well short of --days' default of a year for anything finer
than daily. This command walks backward in windows no larger than
each timeframe's own cap, calling BrokerClient.fetch_recent_candles
repeatedly with an explicit `to_date` (see that method's own docstring)
until --days is covered, instead of one call that would just silently
get clamped to a single window.

Requires BROKER_MODE=live and real ANGEL_ONE_* credentials in .env --
this will raise BrokerAuthError immediately if either is missing, same
as the recurring tasks would.

Usage:
    python manage.py backfill_historical_candles
    python manage.py backfill_historical_candles --symbol NIFTY --timeframe 5m --days 30
    python manage.py backfill_historical_candles --symbol BANKNIFTY --timeframe 1d --days 365
    python manage.py backfill_historical_candles --all-indexes --days 365
    python manage.py backfill_historical_candles --all-indexes --timeframe 1d --days 365
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as django_timezone


class Command(BaseCommand):
    help = (
        "Backfill real OHLCV candles from Angel One for one or many symbol/timeframe "
        "combos (one-off, not the recurring ingestion jobs), chunked to cover a full "
        "--days worth of history even where that exceeds Angel One's per-request cap."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--symbol", default=None,
            help="Single symbol (must exist in broker_client.SYMBOL_TOKENS). "
                 "Default: settings.WATCHLIST. Ignored if --all-indexes is set.",
        )
        parser.add_argument(
            "--all-indexes", action="store_true",
            help="Backfill every apps.investing.models.Index symbol (NIFTY 50/AUTO/BANK/FMCG/"
                 "IT/PHARMA, SENSEX) plus settings.WATCHLIST, not just one symbol.",
        )
        parser.add_argument(
            "--timeframe", default=None,
            choices=["1m", "3m", "5m", "10m", "15m", "30m", "1h", "1d"],
            help="Single timeframe. Default: every timeframe in settings.CHART_TIMEFRAMES.",
        )
        parser.add_argument(
            "--days", type=int, default=365,
            help="Total days of history to backfill, chunked into multiple requests as "
                 "needed per timeframe's Angel One limit (broker_client.MAX_LOOKBACK_DAYS).",
        )

    def handle(self, *args, **options):
        if settings.BROKER_MODE != "live":
            raise CommandError(
                f"BROKER_MODE={settings.BROKER_MODE!r}, not 'live'. Set BROKER_MODE=live and fill in "
                f"ANGEL_ONE_API_KEY/CLIENT_ID/PASSWORD/TOTP_SECRET in .env first, then re-run this."
            )

        from apps.market_data.broker_client import MAX_LOOKBACK_DAYS, BrokerAuthError, BrokerClient
        from apps.market_data.models import HistoricalData

        if options["all_indexes"]:
            from apps.investing.models import Index

            symbols = sorted({*settings.WATCHLIST, *Index.objects.values_list("symbol", flat=True)})
        elif options["symbol"]:
            symbols = [options["symbol"]]
        else:
            symbols = list(settings.WATCHLIST)

        timeframes = [options["timeframe"]] if options["timeframe"] else list(settings.CHART_TIMEFRAMES)
        total_days = options["days"]

        client = BrokerClient()
        grand_total = 0
        failures = []

        for symbol in symbols:
            for timeframe in timeframes:
                window_days = MAX_LOOKBACK_DAYS.get(timeframe, total_days)
                num_chunks = -(-total_days // window_days)  # ceil division
                self.stdout.write(
                    f"{symbol} {timeframe}: backfilling {total_days}d in {num_chunks} "
                    f"chunk(s) of up to {window_days}d each..."
                )

                to_date = django_timezone.localtime(django_timezone.now())
                remaining = total_days
                symbol_tf_inserted = 0
                chunk_num = 0

                while remaining > 0:
                    chunk_days = min(window_days, remaining)
                    chunk_num += 1
                    try:
                        candles = client.fetch_recent_candles(
                            symbol, timeframe, lookback_days=chunk_days, to_date=to_date,
                        )
                    except BrokerAuthError as exc:
                        raise CommandError(f"Angel One login failed: {exc}")
                    except Exception as exc:
                        self.stdout.write(self.style.WARNING(
                            f"  chunk {chunk_num}/{num_chunks} ({chunk_days}d ending {to_date.date()}) "
                            f"failed: {exc} -- skipping this window, continuing with the rest."
                        ))
                        failures.append(f"{symbol}/{timeframe} chunk {chunk_num}")
                        candles = []

                    if candles:
                        with transaction.atomic():
                            created = HistoricalData.objects.bulk_create(
                                [HistoricalData(**c) for c in candles], ignore_conflicts=True,
                            )
                        symbol_tf_inserted += len(created)
                        self.stdout.write(
                            f"  chunk {chunk_num}/{num_chunks} ({chunk_days}d ending {to_date.date()}): "
                            f"{len(candles)} candles returned."
                        )

                    to_date = to_date - timedelta(days=chunk_days)
                    remaining -= chunk_days

                self.stdout.write(self.style.SUCCESS(
                    f"{symbol} {timeframe}: {symbol_tf_inserted} new candle row(s) inserted "
                    f"(rest were already in HistoricalData)."
                ))
                grand_total += symbol_tf_inserted

        summary = (
            f"Done. {grand_total} total new candle row(s) across {len(symbols)} symbol(s) "
            f"x {len(timeframes)} timeframe(s)."
        )
        if failures:
            summary += f" {len(failures)} chunk(s) failed and were skipped: {failures}"
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
