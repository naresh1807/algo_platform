"""
Broadcasts every newly-created HistoricalData row to the
"market_data_live" WebSocket group (apps/market_data/consumers.py).
Kept as a post_save signal (not called explicitly from
apps.market_data.tasks.ingest_watchlist_candles) so ANY code path that
creates a HistoricalData row -- the recurring ingestion task, a manual
backfill script, a Django shell one-off -- automatically pushes live
updates, without every future caller needing to remember to.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from common.websockets import broadcast_group

from .models import HistoricalData

logger = logging.getLogger(__name__)


@receiver(post_save, sender=HistoricalData)
def broadcast_new_candle(sender, instance: HistoricalData, created: bool, **kwargs):
    if kwargs.get("raw", False):
        return  # skip raw bulk inserts/fixtures

    if not created:
        return  # avoid broadcasting every update; live tasks already push the latest candle changes

    broadcast_group(
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
        log=logger,
    )
