"""
Builds live OHLC candles from Angel One SmartWebSocketV2 ticks
(apps/market_data/broker_ws_client.py), in memory, and pushes every
update over the same "market_data_live" Channels group
apps/market_data/signals.py already broadcasts to -- the frontend
(liveStore.js/PriceChart.jsx) needs no changes to consume this, it's
the exact same message shape, just arriving every tick instead of
every 60s.

INTRADAY_LIVE_TIMEFRAMES deliberately excludes "1h"/"1d": this
module anchors every bucket to market open (09:15 IST, the same
anchor apps.market_data.market_hours.MARKET_OPEN_TIME uses and what
Angel One's own candle boundaries are aligned to) so a naive
epoch-second floor doesn't drift -- IST is UTC+5:30, a 30-minute
offset, which divides evenly into 1/3/5/10/15/30-minute buckets but
NOT into a 60-minute bucket. Getting 1h bucket boundaries right would
need a second anchor scheme; simpler to leave 1h/1d on the existing
REST-poll ingestion (apps.market_data.tasks), which is more than
adequate for bars nobody expects to visibly tick anyway.

DB writes are throttled (_MIN_PERSIST_INTERVAL) independently of
broadcasts -- every tick broadcasts immediately (cheap, no DB hit) so
the chart still moves in real time, but MySQL only sees a write a
couple of times a second per symbol/timeframe even under a fast tick
stream. A bucket rollover always persists immediately regardless of
the throttle, so a completed candle is never lost to timing.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone as django_timezone

from .models import HistoricalData

logger = logging.getLogger(__name__)

INTRADAY_LIVE_TIMEFRAMES = ["1m", "3m", "5m", "10m", "15m", "30m"]

TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
}

_MARKET_OPEN_HOUR = 9
_MARKET_OPEN_MINUTE = 15

_MIN_PERSIST_INTERVAL = timedelta(seconds=2)

# Angel One can push several ticks/sec per symbol -- broadcasting
# unthrottled (the original version of this module) meant EVERY tick
# fanned out to group_send() for all 6 live timeframes at once, which
# in practice produced tens of messages/sec per symbol into the
# "market_data_live" Channels group and overran each consumer's queue
# ("N of M channels over capacity" in the Channels/Redis logs -- those
# messages get silently dropped, which is worse than the old 60s poll
# for anyone unlucky enough to have their update dropped). 250ms is
# still 4 updates/sec per symbol/timeframe -- visually smooth, and a
# sane fraction of what any single browser tab can actually render.
_MIN_BROADCAST_INTERVAL = timedelta(milliseconds=250)


def _bucket_start(ts: datetime, timeframe: str) -> datetime:
    ist_ts = django_timezone.localtime(ts)
    market_open = ist_ts.replace(
        hour=_MARKET_OPEN_HOUR, minute=_MARKET_OPEN_MINUTE, second=0, microsecond=0
    )
    interval = TIMEFRAME_SECONDS[timeframe]
    seconds_since_open = (ist_ts - market_open).total_seconds()
    bucket_index = int(seconds_since_open // interval)
    return market_open + timedelta(seconds=bucket_index * interval)


class _BucketState:
    __slots__ = (
        "bucket_start", "open", "high", "low", "close", "volume",
        "_base_day_volume", "last_persisted_at", "last_broadcast_at",
    )

    def __init__(self, bucket_start: datetime, ltp: float, day_volume: int):
        self.bucket_start = bucket_start
        self.open = ltp
        self.high = ltp
        self.low = ltp
        self.close = ltp
        self.volume = 0
        self._base_day_volume = day_volume
        self.last_persisted_at: datetime | None = None
        self.last_broadcast_at: datetime | None = None

    def update(self, ltp: float, day_volume: int) -> None:
        self.high = max(self.high, ltp)
        self.low = min(self.low, ltp)
        self.close = ltp
        # Angel One's day_volume is cumulative for the session, not
        # per-candle -- a bucket's own volume is the delta since the
        # bucket opened. max(0, ...) guards against day_volume resetting
        # (e.g. a fresh session reconnect mid-day where the feed's
        # cumulative counter is briefly stale/lower than our baseline).
        self.volume = max(0, day_volume - self._base_day_volume)

    def as_candle_dict(self, symbol: str, timeframe: str) -> dict:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": self.bucket_start,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class CandleAggregator:
    """
    One instance per process (the run_live_feed management command) --
    not thread-safe by accident, deliberately guarded by _lock since
    SmartWebSocketV2's on_data callback and any future concurrent
    caller could both touch the same bucket state.
    """

    def __init__(self, timeframes: list[str] = INTRADAY_LIVE_TIMEFRAMES):
        self._timeframes = timeframes
        self._state: dict[tuple[str, str], _BucketState] = {}
        self._lock = threading.Lock()

    def on_tick(self, symbol: str, ltp: float, day_volume: int, timestamp: datetime | None = None) -> None:
        ts = timestamp or django_timezone.now()
        for timeframe in self._timeframes:
            self._update_timeframe(symbol, timeframe, ltp, day_volume, ts)

    def _update_timeframe(self, symbol: str, timeframe: str, ltp: float, day_volume: int, ts: datetime) -> None:
        key = (symbol, timeframe)
        bucket_start = _bucket_start(ts, timeframe)
        now = django_timezone.now()

        # Everything that reads/mutates shared state happens under the
        # lock, including snapshotting into plain dicts -- the actual DB
        # write and Channels send (both I/O) happen afterwards, outside
        # the lock, against those snapshots rather than the live
        # (possibly-already-mutated-by-the-next-tick) _BucketState object.
        rollover_candle = None
        with self._lock:
            state = self._state.get(key)
            is_new_bucket = state is None or state.bucket_start != bucket_start
            if is_new_bucket:
                if state is not None:
                    rollover_candle = state.as_candle_dict(symbol, timeframe)
                state = _BucketState(bucket_start, ltp, day_volume)
                self._state[key] = state
            else:
                state.update(ltp, day_volume)

            should_persist = (
                is_new_bucket
                or state.last_persisted_at is None
                or (now - state.last_persisted_at) >= _MIN_PERSIST_INTERVAL
            )
            if should_persist:
                state.last_persisted_at = now

            should_broadcast = (
                is_new_bucket
                or state.last_broadcast_at is None
                or (now - state.last_broadcast_at) >= _MIN_BROADCAST_INTERVAL
            )
            if should_broadcast:
                state.last_broadcast_at = now

            candle = state.as_candle_dict(symbol, timeframe)

        if rollover_candle is not None:
            # Announce the just-closed bar's final values -- separate from
            # `candle` below (the new bar that just opened), both matter:
            # a viewer needs to see the previous bar freeze at its close
            # AND the new bar begin, not just one or the other.
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
            "market_data_live",
            {
                "type": "candle_update",
                "data": {
                    "symbol": candle["symbol"],
                    "timeframe": candle["timeframe"],
                    "timestamp": candle["timestamp"].isoformat(),
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "volume": candle["volume"],
                },
            },
        )

    def _persist(self, candle: dict) -> None:
        try:
            HistoricalData.objects.update_or_create(
                symbol=candle["symbol"],
                timeframe=candle["timeframe"],
                timestamp=candle["timestamp"],
                defaults={
                    "open": Decimal(str(candle["open"])),
                    "high": Decimal(str(candle["high"])),
                    "low": Decimal(str(candle["low"])),
                    "close": Decimal(str(candle["close"])),
                    "volume": candle["volume"],
                    "source": "angel_one_live",
                },
            )
        except Exception:
            # A transient DB hiccup must not kill the live feed process --
            # the next tick's persist attempt (or the Celery beat
            # safety-net poll) will catch up. Broadcasting already happens
            # independently of this, so the UI hasn't stalled even if this
            # particular write failed.
            logger.exception(
                "CandleAggregator: failed to persist %s %s", candle["symbol"], candle["timeframe"]
            )
