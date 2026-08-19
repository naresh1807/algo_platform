"""
Builds live OHLC candles for individual OPTION CONTRACTS from Angel One
SNAP_QUOTE ticks (apps/market_data/broker_ws_client.py's on_option_tick
callback, routed here via apps/options/live_feed.py) -- the option
equivalent of apps.market_data.tick_aggregator.CandleAggregator, keyed
by contract_id instead of a symbol string so every strike/expiry/side
gets its own independent candle stream that can never be mixed with
another contract's or with the underlying's own HistoricalData.

Broadcasts to a per-(contract, timeframe) Channels group
("option_candles_<id>_<timeframe>") rather than one shared group the
way apps/market_data/consumers.py and apps/options/consumers.py's
existing LiveOptionChainConsumer do. That's deliberate: a browser only
ever wants ticks for the exact contract+timeframe its chart is
currently showing, and Channels group membership enforces that at the
transport layer -- apps.options.consumers.OptionCandleConsumer only
joins the ONE group for whatever contract_id/timeframe its own
WebSocket URL names, so a tick for a contract nobody has open never
even reaches a browser that isn't subscribed to it, instead of relying
on every client to correctly filter out every other contract's
messages itself.

The underlying Angel One SmartWebSocketV2 subscription stays exactly as
it already was (apps.market_data.broker_ws_client.LiveFeedClient,
unchanged by this module) -- ONE centralized connection per process
already streams ticks for every currently-synced option contract in the
watchlist's chain (run_live_feed's _load_option_tokens), so no browser
ever causes a new Angel One subscription; this module only decides
which Channels GROUP a given contract's tick fans out to.

Reuses apps.market_data.tick_aggregator's own bucket-boundary math
(_bucket_start/TIMEFRAME_SECONDS/INTRADAY_LIVE_TIMEFRAMES) so an
option's 09:15-anchored bucket boundaries can never drift from the
underlying's -- there must be exactly one definition of "when does a 5m
bucket start," not two that could quietly disagree.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone as django_timezone

from apps.market_data.tick_aggregator import INTRADAY_LIVE_TIMEFRAMES, _bucket_start

logger = logging.getLogger(__name__)

_MIN_PERSIST_INTERVAL = timedelta(seconds=2)
_MIN_BROADCAST_INTERVAL = timedelta(milliseconds=250)


def group_name(contract_id: int, timeframe: str) -> str:
    """The one place this group-naming convention is defined -- apps.options.consumers.OptionCandleConsumer must build the exact same string."""
    return f"option_candles_{contract_id}_{timeframe}"


class _BucketState:
    __slots__ = (
        "bucket_start", "open", "high", "low", "close", "volume",
        "_base_volume", "last_persisted_at", "last_broadcast_at",
    )

    def __init__(self, bucket_start: datetime, ltp: float, cum_volume: int):
        self.bucket_start = bucket_start
        self.open = self.high = self.low = self.close = ltp
        self.volume = 0
        self._base_volume = cum_volume
        self.last_persisted_at: datetime | None = None
        self.last_broadcast_at: datetime | None = None

    def update(self, ltp: float, cum_volume: int) -> None:
        self.high = max(self.high, ltp)
        self.low = min(self.low, ltp)
        self.close = ltp
        # Angel One's day-cumulative volume, same delta-since-bucket-open
        # reasoning as apps.market_data.tick_aggregator._BucketState.
        self.volume = max(0, cum_volume - self._base_volume)

    def as_dict(self, contract_id: int, timeframe: str) -> dict:
        return {
            "contract_id": contract_id, "timeframe": timeframe, "timestamp": self.bucket_start,
            "open": self.open, "high": self.high, "low": self.low, "close": self.close, "volume": self.volume,
        }


class OptionCandleAggregator:
    """One instance shared for the run_live_feed process's whole lifetime -- see module docstring for why."""

    def __init__(self, timeframes: list[str] = INTRADAY_LIVE_TIMEFRAMES):
        self._timeframes = timeframes
        self._state: dict[tuple[int, str], _BucketState] = {}
        self._lock = threading.Lock()

    def on_tick(self, contract_id: int, ltp: float, cum_volume: int, timestamp: datetime | None = None) -> None:
        ts = timestamp or django_timezone.now()
        for timeframe in self._timeframes:
            self._update_timeframe(contract_id, timeframe, ltp, cum_volume, ts)

    def _update_timeframe(self, contract_id: int, timeframe: str, ltp: float, cum_volume: int, ts: datetime) -> None:
        key = (contract_id, timeframe)
        bucket_start = _bucket_start(ts, timeframe)
        now = django_timezone.now()

        rollover_candle = None
        with self._lock:
            state = self._state.get(key)
            is_new_bucket = state is None or state.bucket_start != bucket_start
            if is_new_bucket:
                if state is not None:
                    rollover_candle = state.as_dict(contract_id, timeframe)
                state = _BucketState(bucket_start, ltp, cum_volume)
                self._state[key] = state
            else:
                state.update(ltp, cum_volume)

            should_persist = (
                is_new_bucket or state.last_persisted_at is None
                or (now - state.last_persisted_at) >= _MIN_PERSIST_INTERVAL
            )
            if should_persist:
                state.last_persisted_at = now

            should_broadcast = (
                is_new_bucket or state.last_broadcast_at is None
                or (now - state.last_broadcast_at) >= _MIN_BROADCAST_INTERVAL
            )
            if should_broadcast:
                state.last_broadcast_at = now

            candle = state.as_dict(contract_id, timeframe)

        if rollover_candle is not None:
            self._persist(rollover_candle)
            self._broadcast(rollover_candle)
        if should_broadcast:
            self._broadcast(candle)
        if should_persist:
            self._persist(candle)

    def _broadcast(self, candle: dict) -> None:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            group_name(candle["contract_id"], candle["timeframe"]),
            {
                "type": "candle_update",
                "data": {
                    "contract_id": candle["contract_id"],
                    "timeframe": candle["timeframe"],
                    "timestamp": candle["timestamp"].isoformat(),
                    "open": float(candle["open"]), "high": float(candle["high"]),
                    "low": float(candle["low"]), "close": float(candle["close"]),
                    "volume": candle["volume"],
                },
            },
        )

    def _persist(self, candle: dict) -> None:
        from .models import OptionCandle, OptionContract

        try:
            contract = OptionContract.objects.get(id=candle["contract_id"])
        except OptionContract.DoesNotExist:
            return

        try:
            OptionCandle.objects.update_or_create(
                contract=contract, timeframe=candle["timeframe"], timestamp=candle["timestamp"],
                defaults={
                    "open": Decimal(str(candle["open"])), "high": Decimal(str(candle["high"])),
                    "low": Decimal(str(candle["low"])), "close": Decimal(str(candle["close"])),
                    "volume": candle["volume"], "source": "angel_one_live",
                },
            )
        except Exception:
            # A transient DB hiccup must not kill the live feed process
            # -- same reasoning as apps.market_data.tick_aggregator's own
            # _persist: the next tick's persist attempt (or a later REST
            # candle-history request, which will top up any gap via
            # apps.options.candle_service) catches up.
            logger.exception(
                "OptionCandleAggregator: failed to persist contract_id=%s %s",
                candle["contract_id"], candle["timeframe"],
            )


_aggregator_lock = threading.Lock()
_aggregator: "OptionCandleAggregator | None" = None


def get_option_candle_aggregator() -> OptionCandleAggregator:
    """One aggregator shared for the process lifetime -- same singleton pattern as apps.market_data.broker_client.get_broker_client()."""
    global _aggregator
    with _aggregator_lock:
        if _aggregator is None:
            _aggregator = OptionCandleAggregator()
        return _aggregator
