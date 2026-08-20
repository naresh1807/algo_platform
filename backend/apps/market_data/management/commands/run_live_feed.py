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

Option token selection is apps.options.subscription_manager.
compute_desired_option_tokens, not a raw "every contract for this
underlying" query -- see that module's own docstring for the dropped-
tick incident this fixes (subscribing every synced expiry's every
strike overwhelmed the tick pipeline). broker_ws_client.LiveFeedClient
calls this provider both on every (re)connect AND periodically while
already connected (its own dynamic subscription-refresh loop), so an
expiry rollover or an operator's expiry selection from the UI takes
effect without restarting this process.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand


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
        from apps.options.subscription_manager import compute_desired_option_tokens

        aggregator = CandleAggregator()
        client = LiveFeedClient(
            aggregator,
            on_index_tick=handle_index_tick,
            option_tokens_provider=compute_desired_option_tokens,
            on_option_tick=handle_option_tick,
        )

        self.stdout.write(self.style.SUCCESS("run_live_feed: starting Angel One live tick feed..."))
        try:
            client.run_forever()
        except KeyboardInterrupt:
            self.stdout.write("run_live_feed: stopped.")
