"""
Checks every active PriceAlert against the latest real candle close for
its symbol (apps.market_data.HistoricalData -- same data every other
price-reading feature in this codebase uses, never a separate quote
fetch) and fires a JARVIS announcement (manual 14.16-style proactive
notification) the moment one triggers.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Matches apps.monitoring.health._CELERY_HEARTBEAT_KEYS -- the ONE place
# these key strings are defined, imported by neither module from the
# other to avoid a health<->tasks import cycle; kept identical here on
# purpose (see this module's own tests for a change-detector guard).
_HEARTBEAT_KEY_DEFAULT = "celery:heartbeat:celery"
_HEARTBEAT_KEY_PRIORITY = "celery:heartbeat:priority"


@shared_task
def heartbeat_default_worker():
    """
    Trivial periodic no-op, scheduled on the DEFAULT ('celery') queue
    (config/celery.py beat_schedule) -- its only purpose is proving both
    Celery Beat (which schedules it) and the default worker (which must
    consume it to update the timestamp) are alive, for
    apps.monitoring.health.SystemHealthView.
    """
    cache.set(_HEARTBEAT_KEY_DEFAULT, timezone.now().isoformat(), timeout=settings.CELERY_HEARTBEAT_STALE_SECONDS * 3)
    return {"ok": True}


@shared_task
def heartbeat_priority_worker():
    """
    Same idea as heartbeat_default_worker, routed onto the 'priority'
    queue (config/celery.py task_routes) -- this is what makes fix-list
    item 7 ("priority worker silently not being consumed") an
    observable health-endpoint fact instead of something only noticed
    by reading raw worker logs.
    """
    cache.set(_HEARTBEAT_KEY_PRIORITY, timezone.now().isoformat(), timeout=settings.CELERY_HEARTBEAT_STALE_SECONDS * 3)
    return {"ok": True}


@shared_task
def check_price_alerts():
    from apps.market_data.models import HistoricalData

    from .models import PriceAlert

    triggered = 0
    for alert in PriceAlert.objects.filter(active=True, triggered_at__isnull=True):
        latest = HistoricalData.objects.filter(symbol=alert.symbol).order_by("-timestamp").first()
        if latest is None:
            continue  # no candles for this symbol yet -- nothing to check against

        price = latest.close
        crossed = (
            (alert.condition == PriceAlert.Condition.ABOVE and price >= alert.target_price)
            or (alert.condition == PriceAlert.Condition.BELOW and price <= alert.target_price)
        )
        if not crossed:
            continue

        alert.active = False
        alert.triggered_at = timezone.now()
        alert.triggered_price = price
        alert.save(update_fields=["active", "triggered_at", "triggered_price"])
        triggered += 1

        try:
            from apps.jarvis.notify import announce
            direction = "above" if alert.condition == PriceAlert.Condition.ABOVE else "below"
            announce(
                "price_alert",
                f"{alert.symbol} crossed {direction} {alert.target_price} -- now {price}.",
                symbol=alert.symbol, target_price=str(alert.target_price), price=str(price),
            )
        except Exception:
            logger.exception("Failed to announce triggered price alert %s", alert.pk)

    return {"checked": True, "triggered": triggered}
