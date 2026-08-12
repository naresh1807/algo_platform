"""
Scheduled entry point for the signal engine. Kept as a thin task that
just loops the watchlist and calls generate_signal() per symbol -- all
the actual logic lives in engine.py so it stays independently testable
without needing Celery running at all (call generate_signal() directly
in a test or a Django shell).
"""

import logging

from celery import shared_task
from django.conf import settings

from apps.market_data.market_hours import is_market_open

from .engine import generate_signal

logger = logging.getLogger(__name__)


@shared_task
def generate_signals_for_watchlist(timeframe: str = "5m"):
    """
    Runs on a 5-minute schedule around the clock (config/celery.py
    beat_schedule) but only actually evaluates the watchlist during NSE
    market hours -- see apps.market_data.market_hours.is_market_open.
    Before that helper existed, this ran unconditionally 24/7 (config/
    celery.py's own beat_schedule comment flagged this as a known gap:
    "restrict to NSE market hours... once a market-calendar helper
    exists") -- off-hours runs were harmless (compute_indicators just
    saw no new candles) but wasteful, and worse, could evaluate a
    setup against a stale last-close candle as if it were live,
    producing a signal whose "technical_score" reflected conditions
    from hours or days ago.

    Deliberately does not stop the whole run if one symbol errors --
    logs it and continues, since a data problem with one symbol (e.g.
    a feed gap) shouldn't prevent evaluating the rest of the watchlist.
    """
    market_open, reason = is_market_open()
    if not market_open:
        logger.info("generate_signals_for_watchlist skipped: %s", reason)
        return {"skipped": True, "reason": reason}

    results = []
    for symbol in settings.WATCHLIST:
        try:
            signal = generate_signal(symbol, timeframe)
            results.append({"symbol": symbol, "status": signal.status, "signal_id": signal.pk})
        except Exception:
            logger.exception("generate_signal failed for %s", symbol)
            results.append({"symbol": symbol, "status": "error"})
    return results
