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
from django.utils import timezone

logger = logging.getLogger(__name__)


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
