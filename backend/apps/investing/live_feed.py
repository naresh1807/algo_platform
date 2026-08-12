"""
Drives the dashboard's index ticker cards (IndexTickerBar.jsx) from
real Angel One ticks instead of the REST-poll-only
apps.investing.tasks.sync_index_constituents_and_prices (every ~3
min). apps.market_data.broker_ws_client.LiveFeedClient calls
handle_index_tick() for every tick it receives, for every symbol in
apps.market_data.broker_client.SYMBOL_TOKENS (not just
settings.WATCHLIST -- that restriction only applies to candle
aggregation, see broker_ws_client's own docstring).

Deliberately writes an IndexPriceSnapshot row (throttled) rather than
inventing a new broadcast path: apps/investing/signals.py's post_save
receiver already broadcasts every new IndexPriceSnapshot to the
"index_live" Channels group apps/investing/consumers.py relays and
IndexTickerBar.jsx already consumes -- that whole path needed zero
changes for this to reach the frontend.

`Index.symbol` (e.g. "NIFTY", "BANKNIFTY", "SENSEX") matches
SYMBOL_TOKENS' keys exactly, by that model's own design (see
apps.investing.models.Index's docstring) -- this is what makes the
lookup below a plain dict/queryset match instead of needing its own
mapping table.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone as django_timezone

logger = logging.getLogger(__name__)

_MIN_SNAPSHOT_INTERVAL = timedelta(seconds=2)

_lock = threading.Lock()
_index_by_symbol: dict[str, int] | None = None  # symbol -> Index.id, lazily cached
_last_snapshot_at: dict[str, object] = {}  # symbol -> last snapshot datetime


def _get_index_id(symbol: str):
    global _index_by_symbol

    with _lock:
        if _index_by_symbol is None:
            from .models import Index

            _index_by_symbol = {index.symbol: index.id for index in Index.objects.all()}
        return _index_by_symbol.get(symbol)


def handle_index_tick(symbol: str, ltp: float, close_price: float | None) -> None:
    """
    Called from apps.market_data.broker_ws_client.LiveFeedClient's
    on_data callback for every tick, for every symbol it has a token
    for -- not just the two this platform trades. `close_price` is
    Angel One's previous-close field (see broker_ws_client's QUOTE-mode
    note); None if the feed hasn't sent it yet for this symbol, in
    which case change/change_pct are left blank rather than guessed --
    same "None means unknown, not zero" convention as
    apps.options.broker_client's iv=None.
    """
    index_id = _get_index_id(symbol)
    if index_id is None:
        return  # no Index row for this symbol (e.g. seed_indices hasn't run) -- nothing to update

    now = django_timezone.now()
    with _lock:
        last = _last_snapshot_at.get(symbol)
        if last is not None and (now - last) < _MIN_SNAPSHOT_INTERVAL:
            return
        _last_snapshot_at[symbol] = now

    change = None
    change_pct = None
    if close_price:
        # round() before Decimal for the same reason
        # apps.investing.tasks._sync_index_prices_via_angelone rounds
        # before saving -- raw float subtraction produces binary
        # artifacts (e.g. -65.34999999999854 instead of -65.35), and
        # this value gets broadcast straight from this in-memory object
        # (apps/investing/signals.py sends str(instance.change)) before
        # any DB-side Decimal quantization would otherwise clean it up.
        change = round(ltp - close_price, 2)
        change_pct = round(change / close_price * 100, 4)

    try:
        from .models import IndexPriceSnapshot

        IndexPriceSnapshot.objects.create(
            index_id=index_id,
            timestamp=now,
            ltp=Decimal(str(round(ltp, 2))),
            change=Decimal(str(change)) if change is not None else None,
            change_pct=change_pct,
        )
    except Exception:
        # A transient DB hiccup must not kill the live feed thread --
        # the next tick (2s later at most) retries.
        logger.exception("handle_index_tick: failed to save IndexPriceSnapshot for %s", symbol)
