"""
WebSocket consumer for live option-chain snapshot pushes. Pure relay,
same shape as apps/market_data/consumers.py -- all symbols/expiries
share one group ("options_live"); the frontend filters by
underlying+expiry client-side, since a personal dashboard typically has
only one option chain open at a time and this keeps the backend
group-management trivial (no per-underlying/expiry group churn as the
user switches expiries in the UI).
"""

import json

from common.websockets import AuthenticatedWebsocketConsumer

GROUP_NAME = "options_live"


class LiveOptionChainConsumer(AuthenticatedWebsocketConsumer):
    async def connect(self):
        await self.join_group_and_accept(GROUP_NAME)

    # Called by group_send({"type": "chain_update", ...}) -- Channels
    # maps "type" to a method name; must match apps/options/signals.py exactly.
    async def chain_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))


class OptionCandleConsumer(AuthenticatedWebsocketConsumer):
    """
    ONE group per (contract_id, timeframe) -- deliberately NOT a single
    shared group the way LiveOptionChainConsumer above is. A browser's
    option-contract chart only ever wants ticks for the exact
    contract+timeframe it's currently rendering, and joining a group
    named after exactly that pair (apps.options.candle_aggregator.
    group_name) means a tick for any OTHER contract never even reaches
    this connection -- Channels enforces the isolation the manual's
    "never send a candle to a browser viewing a different token/
    timeframe" requirement asks for, rather than this consumer (or the
    frontend) filtering messages after the fact.

    contract_id/timeframe come straight from the URL path (see
    apps/options/routing.py) -- an unknown/mistyped contract_id just
    joins a group nothing ever broadcasts to (harmless, no messages
    arrive). Authentication is enforced by the shared consumer base
    before the connection can join that group.
    """

    async def connect(self):
        from .candle_aggregator import group_name

        contract_id = self.scope["url_route"]["kwargs"]["contract_id"]
        timeframe = self.scope["url_route"]["kwargs"]["timeframe"]
        await self.join_group_and_accept(group_name(int(contract_id), timeframe))

    # Called by group_send({"type": "candle_update", ...}) -- must match
    # apps.options.candle_aggregator.OptionCandleAggregator._broadcast exactly.
    async def candle_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))
