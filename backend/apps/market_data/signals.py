"""
Broadcasts every newly-created HistoricalData row to the
"market_data_live" WebSocket group (apps/market_data/consumers.py).
Kept as a post_save signal (not called explicitly from
apps.market_data.tasks.ingest_watchlist_candles) so ANY code path that
creates a HistoricalData row -- the recurring ingestion task, a manual
backfill script, a Django shell one-off -- automatically pushes live
updates, without every future caller needing to remember to.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import HistoricalData


@receiver(post_save, sender=HistoricalData)
def broadcast_new_candle(sender, instance: HistoricalData, created: bool, **kwargs):
    if kwargs.get("raw", False):
        return  # skip raw bulk inserts/fixtures

    if not created:
        return  # avoid broadcasting every update; live tasks already push the latest candle changes

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return  # Channels not configured (e.g. some test environments) -- no-op, don't crash

    async_to_sync(channel_layer.group_send)(
        "market_data_live",
        {
            "type": "candle_update",  # must match the consumer method name exactly
            "data": {
                "symbol": instance.symbol,
                "timeframe": instance.timeframe,
                "timestamp": instance.timestamp.isoformat(),
                "open": float(instance.open),
                "high": float(instance.high),
                "low": float(instance.low),
                "close": float(instance.close),
                "volume": instance.volume,
            },
        },
    )
