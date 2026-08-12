"""
Broadcasts every new FeedHealthCheck to the SAME "risk_live" WebSocket
group apps.risk.signals uses (not a separate "monitoring_live" group)
-- the frontend's connection budget is three sockets total
(market-data/signals/risk, see liveStore.js), and feed health is
conceptually part of "is it currently safe to trade", which is what
the risk socket already represents. handleRiskMessage() in
liveStore.js branches on msg.type == "feed_health" specifically for
this.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import FeedHealthCheck


@receiver(post_save, sender=FeedHealthCheck)
def broadcast_feed_health(sender, instance: FeedHealthCheck, created: bool, **kwargs):
    if not created:
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    async_to_sync(channel_layer.group_send)(
        "risk_live",
        {
            "type": "risk_alert",
            "data": {
                "type": "feed_health",
                "source": instance.source,
                "is_healthy": instance.is_healthy,
                "latency_ms": instance.latency_ms,
            },
        },
    )
