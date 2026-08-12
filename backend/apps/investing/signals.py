"""
Broadcasts index/constituent price updates to the "index_live"
WebSocket group (apps/investing/consumers.py). Same pattern as
apps/options/signals.py -- a post_save signal, not a call from
apps.investing.tasks.sync_index_constituents_and_prices directly, so
any code path that saves one of these rows (the recurring task, a
manual shell one-off, a future real broker-WS feed -- see
apps/investing/live_feed.py) automatically pushes a live update
without that code needing to know about Channels at all.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import IndexConstituent, IndexPriceSnapshot

GROUP_NAME = "index_live"


def _send(data: dict) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(GROUP_NAME, {"type": "index_update", "data": data})


@receiver(post_save, sender=IndexPriceSnapshot)
def broadcast_index_price(sender, instance: IndexPriceSnapshot, created: bool, **kwargs):
    if not created:
        return
    _send({
        "kind": "index_price",
        "index_id": instance.index_id,
        "index_name": instance.index.name,
        "ltp": str(instance.ltp),
        "change": str(instance.change) if instance.change is not None else None,
        "change_pct": instance.change_pct,
        "timestamp": instance.timestamp.isoformat(),
    })


@receiver(post_save, sender=IndexConstituent)
def broadcast_constituent_price(sender, instance: IndexConstituent, **kwargs):
    # Every save (not just created) -- update_or_create in
    # sync_index_constituents_and_prices updates the SAME row on every
    # sync cycle rather than inserting a new one (see IndexConstituent's
    # own docstring: it's a live-ish cache, not a time series), so
    # "created" would only fire once per stock, ever.
    _send({
        "kind": "constituent_price",
        "index_id": instance.index_id,
        "stock_symbol": instance.stock.symbol,
        "last_price": str(instance.last_price) if instance.last_price is not None else None,
        "change_pct": instance.change_pct,
        "updated_at": instance.updated_at.isoformat(),
    })
