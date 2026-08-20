"""
Bounded-memory, non-blocking, per-token "latest value wins" mailbox --
the replacement for run_live_feed's old option queue.Queue(maxsize=4000).

Root cause of the reported 5.3M dropped option ticks: a plain FIFO
queue.Queue with a fixed capacity, fed by Angel One's on_data callback
(apps.market_data.broker_ws_client) faster than the single worker
thread that used to drain it could keep up (each tick's processing did
a DB SELECT + a Newton-Raphson IV solve + a DB INSERT -- see
apps.options.live_feed.handle_option_tick's own history). Once the
queue filled, EVERY further tick for EVERY token was dropped and
logged, including tokens whose only backlog was someone else's slow
tick, and there was no way to distinguish "this token's price is
stale" from "some other token backed the whole pipe up."

A TickCoalescer never blocks and never has a fixed capacity to
overflow: put() for a token that already has an unprocessed message
simply REPLACES it (counted as `coalesced`, not `dropped`) rather than
queuing a second entry. For a UI that only ever wants to show the
CURRENT price, the newest tick for a token is strictly more valuable
than an older one still waiting behind it -- there is no reason to ever
process both. This bounds effective queue depth by the number of
DISTINCT actively-ticking tokens (a few hundred at most, per
apps.options.subscription_manager's own strike-range scoping), never by
raw tick volume, which is what actually stops "queue full" drops under
real market load.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)


class TickStats:
    """Thread-safe counters for observability (apps.monitoring's health endpoint reads a snapshot of these)."""

    __slots__ = ("_lock", "received", "processed", "coalesced", "rejected", "dropped", "_lag_samples")

    _MAX_LAG_SAMPLES = 100

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.received = 0
        self.processed = 0
        self.coalesced = 0
        self.rejected = 0
        self.dropped = 0
        self._lag_samples: deque[float] = deque(maxlen=self._MAX_LAG_SAMPLES)

    def record_received(self) -> None:
        with self._lock:
            self.received += 1

    def record_coalesced(self) -> None:
        with self._lock:
            self.coalesced += 1

    def record_rejected(self) -> None:
        with self._lock:
            self.rejected += 1

    def record_dropped(self) -> None:
        with self._lock:
            self.dropped += 1

    def record_processed(self, lag_seconds: float) -> None:
        with self._lock:
            self.processed += 1
            self._lag_samples.append(lag_seconds)

    def snapshot(self, queue_depth: int) -> dict:
        with self._lock:
            lag_ms = int((sum(self._lag_samples) / len(self._lag_samples)) * 1000) if self._lag_samples else 0
            return {
                "received": self.received,
                "processed": self.processed,
                "coalesced": self.coalesced,
                "rejected": self.rejected,
                "dropped": self.dropped,
                "queue_depth": queue_depth,
                "avg_processing_lag_ms": lag_ms,
            }


class TickCoalescer:
    """
    One instance per tick stream (run_live_feed keeps one for options).
    Not a generic pub/sub -- exactly the "per-key latest value" shape
    described in the module docstring above.
    """

    def __init__(self, stats: TickStats | None = None) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending: dict[str, tuple[dict, float]] = {}
        self._order: deque[str] = deque()
        self.stats = stats or TickStats()

    def put(self, token: str, message: dict) -> None:
        """Never blocks, never raises -- see module docstring for why a same-token overwrite is correct, not a loss."""
        self.stats.record_received()
        enqueued_at = time.monotonic()
        with self._cond:
            if token in self._pending:
                self.stats.record_coalesced()
            else:
                self._order.append(token)
            self._pending[token] = (message, enqueued_at)
            self._cond.notify()

    def get(self, timeout: float | None = None):
        """Blocks up to `timeout` seconds for the next distinct token; returns (token, message, enqueued_at_monotonic) or None on timeout."""
        with self._cond:
            if not self._order:
                self._cond.wait(timeout=timeout)
            if not self._order:
                return None
            token = self._order.popleft()
            message, enqueued_at = self._pending.pop(token)
            return token, message, enqueued_at

    def depth(self) -> int:
        with self._lock:
            return len(self._order)


def run_coalescer_workers(
    coalescer: TickCoalescer,
    handler,
    worker_count: int,
    thread_name_prefix: str,
) -> list[threading.Thread]:
    """
    Starts `worker_count` daemon threads, each pulling the next distinct
    token from `coalescer` and calling handler(token, message) --
    several workers can run concurrently since get() only ever hands out
    a given token to ONE caller at a time (it's removed from `_pending`
    the moment it's popped; a tick that arrives WHILE it's being
    processed is queued fresh behind it, never processed out of order
    for the same token, never processed twice for the same message).
    """

    def _worker_loop() -> None:
        while True:
            item = coalescer.get(timeout=1.0)
            if item is None:
                continue
            token, message, enqueued_at = item
            try:
                handler(token, message)
            except Exception:
                logger.exception("TickCoalescer worker: handler failed for token=%s", token)
            finally:
                coalescer.stats.record_processed(time.monotonic() - enqueued_at)

    threads = []
    for i in range(worker_count):
        t = threading.Thread(target=_worker_loop, daemon=True, name=f"{thread_name_prefix}-{i}")
        t.start()
        threads.append(t)
    return threads
