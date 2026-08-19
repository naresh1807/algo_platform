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

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from common.websockets import broadcast_group

from .models import FeedHealthCheck

logger = logging.getLogger(__name__)


@receiver(post_save, sender=FeedHealthCheck)
def broadcast_feed_health(sender, instance: FeedHealthCheck, created: bool, **kwargs):
    if not created:
        return

    broadcast_group(
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
        log=logger,
    )
