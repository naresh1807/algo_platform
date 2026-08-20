"""
apps.market_data.broker_ws_client -- the dynamic subscribe/unsubscribe
manager (fix-list item 2) and the fix for a REAL bug in the installed
SmartApi.smartWebSocketV2.SmartWebSocketV2: `input_request_dict` is a
CLASS attribute (confirmed by reading the installed package's source),
so every instance mutates the SAME shared dict unless given its own,
and its own unsubscribe() corrupts that dict's shape for any future
resubscribe(). _FakeSmartWebSocketV2 below reproduces both behaviors
exactly (not the real network client) so these tests exercise the real
hazard without a network connection.

Every LiveFeedClient instance under test here is built via __new__
(bypassing __init__) and given only the specific instance attributes
each test needs -- __init__ itself starts several background daemon
threads (worker pools, heartbeat, subscription refresh) that have no
place running inside a fast, deterministic unit test.
"""

import threading
import unittest

from apps.market_data.broker_ws_client import (
    SUBSCRIBE_MODE_SNAP_QUOTE,
    LiveFeedClient,
    _dedupe_input_request_dict,
    _repair_input_request_dict_after_unsubscribe,
)


class _FakeSmartWebSocketV2:
    """Reproduces SmartWebSocketV2's real class-level input_request_dict sharing + unsubscribe() corruption bug."""

    input_request_dict = {}  # CLASS attribute -- deliberately shared unless overridden per-instance, matching the installed package.

    def __init__(self):
        self.sent = []

    def subscribe(self, correlation_id, mode, token_list):
        if self.input_request_dict.get(mode) is None:
            self.input_request_dict[mode] = {}
        for token in token_list:
            if token["exchangeType"] in self.input_request_dict[mode]:
                self.input_request_dict[mode][token["exchangeType"]].extend(token["tokens"])
            else:
                self.input_request_dict[mode][token["exchangeType"]] = token["tokens"]
        self.sent.append(("subscribe", correlation_id, mode, token_list))

    def unsubscribe(self, correlation_id, mode, token_list):
        # Reproduces the real installed-library bug: merges top-level
        # keys directly into the SAME dict subscribe() uses.
        request_data = {"correlationID": correlation_id, "action": 0, "params": {"mode": mode, "tokenList": token_list}}
        self.input_request_dict.update(request_data)
        self.sent.append(("unsubscribe", correlation_id, mode, token_list))


def _make_client_stub(option_tokens=None, provider=None, sws=None, ready=True):
    client = LiveFeedClient.__new__(LiveFeedClient)
    client._sub_lock = threading.Lock()
    client._option_tokens = option_tokens or {}
    client._active_sws = sws
    client._ws_ready = threading.Event()
    if ready:
        client._ws_ready.set()
    client._option_tokens_provider = provider
    return client


class InputRequestDictBugFixTests(unittest.TestCase):
    def setUp(self):
        _FakeSmartWebSocketV2.input_request_dict.clear()

    def test_without_the_per_instance_override_instances_leak_state(self):
        """Demonstrates the actual bug this codebase works around -- proves the hazard is real."""
        sws1 = _FakeSmartWebSocketV2()
        sws1.subscribe("cid1", SUBSCRIBE_MODE_SNAP_QUOTE, [{"exchangeType": 2, "tokens": ["1"]}])
        sws2 = _FakeSmartWebSocketV2()
        self.assertIn(SUBSCRIBE_MODE_SNAP_QUOTE, sws2.input_request_dict)  # leaked from sws1, unrelated instance

    def test_per_instance_override_prevents_cross_instance_leakage(self):
        """The fix apps.market_data.broker_ws_client._connect_and_subscribe applies: `sws.input_request_dict = {}`."""
        sws1 = _FakeSmartWebSocketV2()
        sws1.input_request_dict = {}
        sws1.subscribe("cid1", SUBSCRIBE_MODE_SNAP_QUOTE, [{"exchangeType": 2, "tokens": ["1", "2"]}])

        sws2 = _FakeSmartWebSocketV2()
        sws2.input_request_dict = {}
        self.assertEqual(sws2.input_request_dict, {})

    def test_unsubscribe_corruption_is_repaired(self):
        sws = _FakeSmartWebSocketV2()
        sws.input_request_dict = {}
        sws.subscribe("cid1", SUBSCRIBE_MODE_SNAP_QUOTE, [{"exchangeType": 2, "tokens": ["1", "2", "3"]}])

        token_list = [{"exchangeType": 2, "tokens": ["2"]}]
        sws.unsubscribe("cid2", SUBSCRIBE_MODE_SNAP_QUOTE, token_list)
        self.assertIn("correlationID", sws.input_request_dict)  # the bug, before repair

        _repair_input_request_dict_after_unsubscribe(sws, SUBSCRIBE_MODE_SNAP_QUOTE, token_list)

        self.assertNotIn("correlationID", sws.input_request_dict)
        self.assertNotIn("action", sws.input_request_dict)
        self.assertNotIn("params", sws.input_request_dict)
        self.assertEqual(sws.input_request_dict[SUBSCRIBE_MODE_SNAP_QUOTE][2], ["1", "3"])

    def test_dedupe_removes_duplicate_tokens_preserving_order(self):
        sws = _FakeSmartWebSocketV2()
        sws.input_request_dict = {SUBSCRIBE_MODE_SNAP_QUOTE: {2: ["1", "2", "1", "3", "2"]}}
        _dedupe_input_request_dict(sws, SUBSCRIBE_MODE_SNAP_QUOTE)
        self.assertEqual(sws.input_request_dict[SUBSCRIBE_MODE_SNAP_QUOTE][2], ["1", "2", "3"])


class SubscriptionRefreshTests(unittest.TestCase):
    def setUp(self):
        _FakeSmartWebSocketV2.input_request_dict.clear()

    def test_refresh_is_a_noop_when_nothing_is_connected(self):
        client = _make_client_stub(sws=None, ready=False, provider=lambda: {"1": {"contract_id": 1}})
        client._refresh_subscription()  # must not raise
        self.assertEqual(client._option_tokens, {})

    def test_refresh_subscribes_added_tokens_and_unsubscribes_removed_ones(self):
        sws = _FakeSmartWebSocketV2()
        sws.input_request_dict = {}
        client = _make_client_stub(
            option_tokens={"1": {"contract_id": 1}, "2": {"contract_id": 2}},
            provider=lambda: {"2": {"contract_id": 2}, "3": {"contract_id": 3}},
            sws=sws,
        )

        client._refresh_subscription()

        actions = [entry[0] for entry in sws.sent]
        self.assertIn("subscribe", actions)
        self.assertIn("unsubscribe", actions)
        subscribed_tokens = next(entry[3] for entry in sws.sent if entry[0] == "subscribe")[0]["tokens"]
        unsubscribed_tokens = next(entry[3] for entry in sws.sent if entry[0] == "unsubscribe")[0]["tokens"]
        self.assertEqual(subscribed_tokens, ["3"])
        self.assertEqual(unsubscribed_tokens, ["1"])
        self.assertEqual(client._option_tokens, {"2": {"contract_id": 2}, "3": {"contract_id": 3}})

    def test_refresh_does_nothing_when_desired_set_is_unchanged(self):
        sws = _FakeSmartWebSocketV2()
        sws.input_request_dict = {}
        client = _make_client_stub(
            option_tokens={"1": {"contract_id": 1}},
            provider=lambda: {"1": {"contract_id": 1}},
            sws=sws,
        )

        client._refresh_subscription()

        self.assertEqual(sws.sent, [])

    def test_reconnect_never_leaves_duplicate_tokens_across_two_refreshes(self):
        sws = _FakeSmartWebSocketV2()
        sws.input_request_dict = {}
        client = _make_client_stub(
            option_tokens={},
            provider=lambda: {"1": {"contract_id": 1}, "2": {"contract_id": 2}},
            sws=sws,
        )
        client._refresh_subscription()
        client._refresh_subscription()  # same desired set again -- must not re-subscribe

        subscribe_calls = [entry for entry in sws.sent if entry[0] == "subscribe"]
        self.assertEqual(len(subscribe_calls), 1)
