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

from channels.generic.websocket import AsyncWebsocketConsumer

GROUP_NAME = "options_live"


class LiveOptionChainConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

    # Called by group_send({"type": "chain_update", ...}) -- Channels
    # maps "type" to a method name; must match apps/options/signals.py exactly.
    async def chain_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))
