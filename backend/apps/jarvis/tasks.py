"""
Scheduled JARVIS announcements (manual 14.16: "Market Open" / "Market
Close") that aren't triggered by another app's model save -- there's no
MarketSession model to hook a post_save signal onto, so these are
plain Celery beat tasks instead, scheduled directly in config/celery.py
at NSE's session boundaries (09:15 / 15:30 IST, Mon-Fri only, via
crontab's day_of_week filter -- see celery.py for why that's enough
and no in-task weekday/holiday check is added on top: a holiday still
firing this is a harmless false announcement, not a safety issue, and
NSE's actual holiday calendar isn't data this scaffold has anywhere).
"""

from __future__ import annotations

from celery import shared_task
from django.conf import settings

from .notify import announce


@shared_task
def announce_market_open() -> str:
    watchlist = ", ".join(getattr(settings, "WATCHLIST", [])) or "no watchlist configured"
    message = f"Market open -- NSE session started. Watchlist: {watchlist}."
    announce("market_open", message)
    return message


@shared_task
def announce_market_close() -> str:
    message = "Market close -- NSE session ended for the day."
    announce("market_close", message)
    return message


@shared_task
def generate_market_outlook() -> dict:
    """
    apps.jarvis.market_intelligence.market_outlook(), run periodically
    during market hours and stored as a MarketOutlookSnapshot -- the
    "ML/JARVIS continuously observes everything" the trader asked for.
    Only fires a JARVIS announcement when the overall bias CHANGES from
    the previous snapshot (Neutral -> Bullish, etc.), not on every run
    -- announcing "Market bias: Neutral" every 15 minutes all day would
    train a trader to ignore JARVIS announcements entirely, defeating
    the point of manual 14.16's proactive-notification design this
    whole announce() pipeline is built around.
    """
    from .market_intelligence import market_outlook
    from .models import MarketOutlookSnapshot

    result = market_outlook()

    previous = MarketOutlookSnapshot.objects.order_by("-generated_at").first()

    options_label = ""
    if result["options_idea"] and result["options_idea"].get("available"):
        oi = result["options_idea"]
        options_label = f"{oi['underlying']} {oi['strike']} {'CE' if oi['direction'] == 'bullish' else 'PE'}"

    stock_label = result["stock_idea"]["symbol"] if result["stock_idea"] else ""

    snapshot = MarketOutlookSnapshot.objects.create(
        bias=result["bias"],
        bias_confidence=result["bias_confidence"],
        summary=result["summary"],
        best_options_idea=options_label,
        best_stock_idea=stock_label,
    )

    if previous is not None and previous.bias != snapshot.bias:
        announce(
            "market_outlook_change",
            f"Market bias changed from {previous.bias} to {snapshot.bias}. {snapshot.summary}",
        )

    return {"bias": snapshot.bias, "changed": previous is not None and previous.bias != snapshot.bias}
