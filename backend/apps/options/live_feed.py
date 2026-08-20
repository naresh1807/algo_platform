"""
Drives live option-chain premium/OI/bid-ask movement from real Angel
One ticks, the option-chain equivalent of
apps.investing.live_feed.handle_index_tick.
apps.market_data.broker_ws_client.LiveFeedClient calls
handle_option_tick() for every SNAP_QUOTE tick it receives for a
subscribed option contract (see that module's `on_option_tick`).

FAST/SLOW SPLIT (this is the fix for "the UI must receive LTP before
DB/IV processing," and for the tick-drop incident described in
apps.market_data.broker_ws_client's own module docstring): every tick
first broadcasts a lightweight LTP update straight from the tick's own
fields and the caller-supplied contract identity (`meta`, resolved once
by apps.options.subscription_manager -- no DB query on this path at
all), THEN does the slower work (candle aggregation, throttled
OptionChainSnapshot persistence, local IV solve). A DB hiccup or an IV
solver failure in the slow path can never block or delay the fast
broadcast that already happened before either ran.

The fast broadcast reuses the SAME "chain_update" message shape and
Channels group ("options_live") the throttled OptionChainSnapshot path
already broadcasts via apps/options/signals.py's post_save receiver --
deliberately not a new message type -- so the existing frontend merge
(OptionsAnalytics.jsx / JarvisSignalTerminal.jsx) needs no new handler,
only a field-level merge instead of a whole-leg replace (see those
files' own comments). Fields the fast path cannot know yet
(change_in_oi, iv, greeks -- all need a DB read or a real computation)
are OMITTED from the payload entirely, not set to null, specifically so
the frontend's merge preserves whatever the last full snapshot said
instead of blanking those columns out between snapshots.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.utils import timezone as django_timezone

logger = logging.getLogger(__name__)

# Matches apps.market_data.tick_aggregator's _MIN_PERSIST_INTERVAL --
# an index option chain can have 100+ live contracts, and Angel One can
# tick several times a second per contract, so this bounds DB
# writes/full-snapshot broadcasts to a sane rate regardless of tick
# volume. Does NOT throttle the fast LTP broadcast above -- that runs on
# every tick.
_MIN_SNAPSHOT_INTERVAL = timedelta(seconds=2)

_lock = threading.Lock()
_last_snapshot_at: dict[int, object] = {}  # contract_id -> last snapshot datetime


def _broadcast_fast_ltp(meta: dict, ltp: float, oi: int, volume: int, bid: float | None, ask: float | None, now) -> None:
    from common.websockets import broadcast_group

    broadcast_group(
        "options_live",
        {
            "type": "chain_update",  # must match apps.options.consumers.LiveOptionChainConsumer's method name
            "data": {
                "contract_id": meta["contract_id"],
                "underlying": meta["underlying"],
                "expiry": meta["expiry"],
                "strike": meta["strike"],
                "option_type": meta["option_type"],
                "timestamp": now.isoformat(),
                "ltp": ltp,
                "open_interest": oi,
                "volume": volume,
                "bid": bid,
                "ask": ask,
                # change_in_oi / iv / greeks deliberately absent -- see
                # module docstring. The slow-path OptionChainSnapshot
                # broadcast (apps/options/signals.py) fills them in
                # moments later without the frontend ever showing a
                # blank/reset value in between.
            },
        },
        log=logger,
    )


def handle_option_tick(
    meta: dict, ltp: float, oi: int, volume: int, bid: float | None, ask: float | None,
    exchange_timestamp_ms: int | None = None,
) -> None:
    """
    `meta`: {"contract_id", "underlying", "expiry" (ISO string),
    "strike" (float), "option_type"} -- resolved once by
    apps.options.subscription_manager when the token was subscribed, not
    re-fetched from the DB on every tick (see this module's own
    docstring for why that matters).
    """
    now = django_timezone.now()
    contract_id = meta["contract_id"]

    try:
        _broadcast_fast_ltp(meta, ltp, oi, volume, bid, ask, now)
    except Exception:
        logger.exception("handle_option_tick: fast LTP broadcast failed for contract_id=%s", contract_id)

    # Candle aggregation runs on EVERY tick, unthrottled by the
    # OptionChainSnapshot interval below -- apps.options.candle_aggregator
    # .OptionCandleAggregator does its own internal persist/broadcast
    # throttling, the same "every tick updates the in-memory bucket, DB
    # writes are rate limited independently" split
    # apps.market_data.tick_aggregator already uses for the underlying's
    # own live candles.
    try:
        from .candle_aggregator import get_option_candle_aggregator

        get_option_candle_aggregator().on_tick(contract_id, ltp, volume, now)
    except Exception:
        logger.exception("handle_option_tick: candle aggregation failed for contract_id=%s", contract_id)

    with _lock:
        last = _last_snapshot_at.get(contract_id)
        if last is not None and (now - last) < _MIN_SNAPSHOT_INTERVAL:
            return
        _last_snapshot_at[contract_id] = now

    from apps.market_data.models import HistoricalData

    from .greeks import DEFAULT_RISK_FREE_RATE, implied_volatility
    from .models import OptionChainSnapshot, OptionContract

    try:
        contract = OptionContract.objects.get(id=contract_id)
    except OptionContract.DoesNotExist:
        return  # a contract that was subscribed at startup but has since been removed (rare, harmless)

    previous = OptionChainSnapshot.objects.filter(contract=contract).order_by("-timestamp").first()
    previous_oi = previous.open_interest if previous else 0

    # Same local-IV-solve fallback as ingest_option_chain_snapshots
    # (apps/options/tasks.py) -- Angel One's tick payload has no IV
    # field either, same documented gap as its REST quote payload. A
    # failure here must never block the fast LTP broadcast above -- it
    # already happened, unconditionally, before this line ever runs.
    iv = None
    latest_underlying = HistoricalData.objects.filter(symbol=contract.underlying).order_by("-timestamp").first()
    spot = float(latest_underlying.close) if latest_underlying else None
    if spot is not None:
        try:
            tte_years = (contract.expiry - django_timezone.localdate()).days / 365.0
            solved = implied_volatility(ltp, spot, float(contract.strike), tte_years, DEFAULT_RISK_FREE_RATE, contract.option_type)
            iv = round(solved * 100, 2) if solved is not None else None
        except Exception:
            logger.exception("handle_option_tick: local IV solve failed for contract_id=%s", contract_id)

    try:
        OptionChainSnapshot.objects.create(
            contract=contract,
            timestamp=now,
            ltp=ltp,
            open_interest=oi,
            change_in_oi=oi - previous_oi,
            volume=volume,
            iv=iv,
            bid=bid,
            ask=ask,
        )
    except Exception:
        logger.exception("handle_option_tick: failed to save OptionChainSnapshot for contract_id=%s", contract_id)
