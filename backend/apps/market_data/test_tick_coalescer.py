"""
apps.market_data.tick_coalescer -- the replacement for the fixed-size
option tick queue.Queue that produced the platform's real 5.3M-dropped-
tick incident (fix-list items 3 and 4). Pure in-memory, no Django DB/
Redis dependency -- plain unittest.TestCase, not django.test.TestCase.
"""

import threading
import time
import unittest

from apps.market_data.tick_coalescer import TickCoalescer, TickStats, run_coalescer_workers


class TickCoalescerTests(unittest.TestCase):
    def test_put_never_blocks_under_a_burst_for_the_same_token(self):
        coalescer = TickCoalescer()
        started = time.monotonic()
        for i in range(5000):
            coalescer.put("TOKEN-A", {"last_traded_price": i})
        elapsed = time.monotonic() - started
        # A blocking/bounded queue.Queue(maxsize=4000) would either raise
        # queue.Full or block on the 4001st put for this exact scenario
        # (same token, no consumer draining). This must complete near-
        # instantly and never raise.
        self.assertLess(elapsed, 1.0)
        self.assertEqual(coalescer.depth(), 1)  # one distinct token, coalesced down to its latest message

    def test_newest_tick_replaces_stale_unprocessed_tick_for_same_token(self):
        coalescer = TickCoalescer()
        coalescer.put("TOKEN-A", {"last_traded_price": 1})
        coalescer.put("TOKEN-A", {"last_traded_price": 2})
        coalescer.put("TOKEN-A", {"last_traded_price": 3})

        token, message, _ = coalescer.get(timeout=1.0)
        self.assertEqual(token, "TOKEN-A")
        self.assertEqual(message["last_traded_price"], 3)
        self.assertEqual(coalescer.stats.coalesced, 2)
        self.assertEqual(coalescer.stats.received, 3)

    def test_distinct_tokens_are_not_coalesced_together(self):
        coalescer = TickCoalescer()
        coalescer.put("TOKEN-A", {"last_traded_price": 1})
        coalescer.put("TOKEN-B", {"last_traded_price": 2})

        self.assertEqual(coalescer.depth(), 2)
        seen = set()
        for _ in range(2):
            token, _message, _ = coalescer.get(timeout=1.0)
            seen.add(token)
        self.assertEqual(seen, {"TOKEN-A", "TOKEN-B"})

    def test_get_returns_none_on_timeout_when_empty(self):
        coalescer = TickCoalescer()
        self.assertIsNone(coalescer.get(timeout=0.05))

    def test_workers_process_every_distinct_token_exactly_once_per_message(self):
        coalescer = TickCoalescer()
        processed = []
        lock = threading.Lock()

        def handler(token, message):
            with lock:
                processed.append((token, message["last_traded_price"]))

        run_coalescer_workers(coalescer, handler, worker_count=4, thread_name_prefix="test-worker")

        for i in range(50):
            coalescer.put(f"TOKEN-{i}", {"last_traded_price": i})

        deadline = time.monotonic() + 5.0
        while len(processed) < 50 and time.monotonic() < deadline:
            time.sleep(0.02)

        self.assertEqual(len(processed), 50)
        self.assertEqual(set(processed), {(f"TOKEN-{i}", i) for i in range(50)})
        self.assertEqual(coalescer.stats.processed, 50)


class TickStatsTests(unittest.TestCase):
    def test_snapshot_reports_all_counters(self):
        stats = TickStats()
        stats.record_received()
        stats.record_received()
        stats.record_coalesced()
        stats.record_rejected()
        stats.record_dropped()
        stats.record_processed(0.01)

        snapshot = stats.snapshot(queue_depth=3)
        self.assertEqual(snapshot["received"], 2)
        self.assertEqual(snapshot["processed"], 1)
        self.assertEqual(snapshot["coalesced"], 1)
        self.assertEqual(snapshot["rejected"], 1)
        self.assertEqual(snapshot["dropped"], 1)
        self.assertEqual(snapshot["queue_depth"], 3)
        self.assertGreaterEqual(snapshot["avg_processing_lag_ms"], 0)
