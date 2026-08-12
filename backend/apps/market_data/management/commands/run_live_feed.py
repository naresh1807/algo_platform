"""
python manage.py run_live_feed

Long-running process (like `celery worker`/`celery beat` -- run this
in its own terminal, it never returns): opens the persistent Angel One
SmartWebSocketV2 tick connection (apps.market_data.broker_ws_client)
and feeds it into live candle aggregation
(apps.market_data.tick_aggregator), the index ticker's live LTP
(apps.investing.live_feed), and live option-chain premium/OI/bid-ask
movement (apps.options.live_feed). This is what makes the dashboard's
chart, index cards, and option chain move tick-by-tick instead of on
the Celery beat ingestion schedule (config/celery.py) -- see
apps/investing/live_feed.py, apps/options/live_feed.py, and
apps/market_data/broker_ws_client.py for the full explanation of why
the REST-poll ingestion alone could never do this.

Same BROKER_MODE=live guard every other Angel-One-dependent entry
point in this codebase uses (apps.market_data.tasks,
apps.investing.tasks) -- there is no broker session to stream from in
paper mode.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand


def _load_option_tokens() -> dict[str, int]:
    """
    symbol_token -> OptionContract.id for every contract currently
    synced against settings.WATCHLIST -- called fresh on every
    (re)connect (see LiveFeedClient.run_forever), so a weekly
    sync_watchlist_option_contracts rollover to a new nearest expiry is
    picked up without restarting this process. Empty (not an error)
    until that weekly sync has populated OptionContract at all, or
    outside BROKER_MODE=live where nothing populates it -- an empty
    option subscription just means candles/index prices still stream
    normally, matching this module's own "missing config is a skip,
    not a crash" convention elsewhere.
    """
    from apps.options.models import OptionContract

    return dict(
        OptionContract.objects.filter(underlying__in=settings.WATCHLIST).values_list("symbol_token", "id")
    )


class Command(BaseCommand):
    help = "Runs the persistent Angel One live-tick WebSocket feed (candles + index prices). Long-running -- does not return."

    def handle(self, *args, **options):
        if settings.BROKER_MODE != "live":
            self.stderr.write(
                self.style.WARNING(
                    f"run_live_feed: BROKER_MODE={settings.BROKER_MODE!r} -- set BROKER_MODE=live "
                    "with real ANGEL_ONE_* credentials in .env to actually stream ticks. Exiting."
                )
            )
            return

        from apps.investing.live_feed import handle_index_tick
        from apps.market_data.broker_ws_client import LiveFeedClient
        from apps.market_data.tick_aggregator import CandleAggregator
        from apps.options.live_feed import handle_option_tick

        aggregator = CandleAggregator()
        client = LiveFeedClient(
            aggregator,
            on_index_tick=handle_index_tick,
            option_tokens_provider=_load_option_tokens,
            on_option_tick=handle_option_tick,
        )

        self.stdout.write(self.style.SUCCESS("run_live_feed: starting Angel One live tick feed..."))
        try:
            client.run_forever()
        except KeyboardInterrupt:
            self.stdout.write("run_live_feed: stopped.")
