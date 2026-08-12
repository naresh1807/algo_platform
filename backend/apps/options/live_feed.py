"""
Drives live option-chain premium/OI/bid-ask movement from real Angel
One ticks, the option-chain equivalent of
apps.investing.live_feed.handle_index_tick.
apps.market_data.broker_ws_client.LiveFeedClient calls
handle_option_tick() for every SNAP_QUOTE tick it receives for a
subscribed option contract (see that module's `on_option_tick`).

Deliberately writes an OptionChainSnapshot row (throttled) rather than
inventing a new broadcast path: apps/options/signals.py's post_save
receiver already broadcasts every new OptionChainSnapshot to the
"options_live" Channels group (computing Greeks server-side too) --
that whole path needed zero changes for this to reach the frontend,
same reasoning apps.investing.live_feed gives for IndexPriceSnapshot.

The per-snapshot fields (change_in_oi vs. the previous snapshot, local
IV solve when the broker doesn't supply one) are the SAME two
computations apps.options.tasks.ingest_option_chain_snapshots already
does for its REST-poll path -- reused here, not reimplemented, so the
live and polled paths can never silently compute these differently.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.utils import timezone as django_timezone

logger = logging.getLogger(__name__)

# Matches apps.market_data.tick_aggregator's _MIN_PERSIST_INTERVAL --
# same reasoning: an index option chain can have 100+ live contracts,
# and Angel One can tick several times a second per contract, so this
# bounds DB writes/broadcasts to a sane rate regardless of tick volume.
_MIN_SNAPSHOT_INTERVAL = timedelta(seconds=2)

_lock = threading.Lock()
_last_snapshot_at: dict[int, object] = {}  # contract_id -> last snapshot datetime


def handle_option_tick(contract_id: int, ltp: float, oi: int, volume: int, bid: float | None, ask: float | None) -> None:
    now = django_timezone.now()
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
    # field either, same documented gap as its REST quote payload.
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
