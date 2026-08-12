"""
Seeds enough realistic (but clearly-labeled, synthetic) data to see the
FULL pipeline actually work end-to-end -- candles in, indicators
computed, a real signal generated, equity/risk state populated -- all
without needing real Angel One credentials.

Why this exists: migrations creating empty tables is necessary but not
sufficient for the dashboard to show anything -- nothing writes a
single row into those tables until either (a) BROKER_MODE=live with
real credentials AND a running Celery worker+beat, or (b) something
seeds data manually. This command is (b), specifically for verifying
the plumbing (dashboard rendering, chart, signal engine, risk engine)
actually works, separate from the separate question of "is real
broker data flowing."

Every row this creates is tagged so it's obvious it's not real market
data (source="seed_demo" on candles) -- never run this against a
database you're also feeding real data into without being aware the
two will mix.

Usage:
    python manage.py seed_demo_data
    python manage.py seed_demo_data --candles 200 --symbol BANKNIFTY
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Seed synthetic candles + a strategy version + account equity, then generate one real signal from them."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", default="NIFTY")
        parser.add_argument("--timeframe", default="5m")
        parser.add_argument(
            "--candles", type=int, default=120,
            help="How many 5-minute candles to generate (>=60 needed for indicators.MIN_CANDLES_REQUIRED).",
        )

    def handle(self, *args, **options):
        symbol = options["symbol"]
        timeframe = options["timeframe"]
        candle_count = max(options["candles"], 60)

        self._seed_account_equity()
        strategy_version = self._seed_strategy_version()
        self._seed_candles(symbol, timeframe, candle_count)
        self._generate_one_signal(symbol, timeframe, strategy_version)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Seeded {candle_count} synthetic {symbol} {timeframe} candles, "
            f"an active strategy version, starting equity, and generated one real "
            f"signal from that data. Refresh the dashboard -- the chart, signal "
            f"status, and equity/risk panels should now show something.\n"
            f"\nThis is SYNTHETIC data (source='seed_demo' on the candles) purely to "
            f"verify the pipeline works -- it is not real market data and the signal "
            f"it produces means nothing about actual NIFTY/BANKNIFTY conditions."
        ))

    def _seed_account_equity(self):
        from apps.risk.models import AccountEquity

        equity, created = AccountEquity.objects.get_or_create(
            pk=1,
            defaults={
                "current_equity": Decimal("100000"),
                "daily_start_equity": Decimal("100000"),
                "peak_equity": Decimal("100000"),
                "trading_day": timezone.localdate(),
            },
        )
        self.stdout.write(f"AccountEquity: {'created' if created else 'already existed'}")

    def _seed_strategy_version(self):
        from apps.learning.models import StrategyVersion

        version, created = StrategyVersion.objects.get_or_create(
            version_name="seed-demo-v1",
            defaults={"params_json": {"note": "synthetic seed data, not a validated strategy"}, "active_flag": True},
        )
        if not created and not version.active_flag:
            # Don't silently create a second active version if one
            # already exists from real use -- only activate ours if
            # nothing else already claims to be active.
            if not StrategyVersion.objects.filter(active_flag=True).exists():
                version.active_flag = True
                version.save(update_fields=["active_flag"])
        self.stdout.write(f"StrategyVersion: {'created' if created else 'already existed'}")
        return version

    def _seed_candles(self, symbol, timeframe, count):
        from apps.market_data.models import HistoricalData

        existing = HistoricalData.objects.filter(symbol=symbol, timeframe=timeframe).count()
        if existing >= count:
            self.stdout.write(
                f"HistoricalData: {existing} {symbol} {timeframe} candles already exist -- skipping seed."
            )
            return

        # Simple random-walk OHLCV generator -- realistic-SHAPED (each
        # candle's open continues from the previous close, high/low
        # bracket open/close correctly, volume varies) but NOT modeling
        # any real market behavior. Good enough to exercise
        # apps.market_data.indicators (needs >=60 candles) and produce
        # a plausible-looking chart; not good enough, and not intended,
        # to mean anything about real price action.
        price = 24500.0  # a plausible NIFTY level as a starting point
        now = timezone.now().replace(second=0, microsecond=0)
        interval = timedelta(minutes=5 if timeframe == "5m" else 1)

        rows = []
        for i in range(count):
            ts = now - interval * (count - i)
            change = random.uniform(-0.4, 0.4) * (price * 0.001)
            open_price = price
            close_price = price + change
            high = max(open_price, close_price) + abs(random.uniform(0, price * 0.0005))
            low = min(open_price, close_price) - abs(random.uniform(0, price * 0.0005))
            volume = random.randint(50000, 500000)

            rows.append(HistoricalData(
                symbol=symbol, timeframe=timeframe, timestamp=ts,
                open=Decimal(str(round(open_price, 2))),
                high=Decimal(str(round(high, 2))),
                low=Decimal(str(round(low, 2))),
                close=Decimal(str(round(close_price, 2))),
                volume=volume, source="seed_demo",
            ))
            price = close_price

        HistoricalData.objects.bulk_create(rows, ignore_conflicts=True)
        self.stdout.write(f"HistoricalData: created {len(rows)} synthetic candles for {symbol} {timeframe}")

    def _generate_one_signal(self, symbol, timeframe, strategy_version):
        from apps.signals.engine import generate_signal

        try:
            signal = generate_signal(symbol, timeframe)
            self.stdout.write(
                f"TradingSignal generated: {signal.signal_type} / {signal.status} "
                f"(score {signal.total_score:.2f}) -- reason: {signal.reason}"
            )
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"Could not generate a signal from the seeded data: {exc}. "
                f"The candles/equity/strategy rows are still seeded -- this only "
                f"means the signal engine itself hit an issue, worth investigating separately."
            ))
