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

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from common.websockets import broadcast_group

from .models import IndexConstituent, IndexPriceSnapshot

GROUP_NAME = "index_live"
logger = logging.getLogger(__name__)


def _send(data: dict) -> None:
    broadcast_group(
        GROUP_NAME,
        {"type": "index_update", "data": data},
        log=logger,
    )


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
