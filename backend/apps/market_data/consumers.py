"""
WebSocket consumer for live candle data. All symbols broadcast into one
shared group ("market_data_live") rather than a per-symbol group --
matches how the frontend's liveStore.js already connects to a single
fixed /ws/market-data/live/ endpoint and filters by symbol client-side
in the message payload, since the watchlist is small (settings.WATCHLIST,
just 2 symbols by default) and a personal dashboard is typically
watching more than one of them at once anyway. apps.market_data.signals
broadcasts to this same group name whenever a new HistoricalData row is
saved, so this consumer itself contains no polling loop or business
logic at all -- it's a pure relay between the channel layer and the
browser.
"""

import json

from channels.generic.websocket import AsyncWebsocketConsumer

GROUP_NAME = "market_data_live"


class LiveCandleConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

    # Called by group_send({"type": "candle_update", ...}) -- Channels
    # maps the "type" key to a method name by replacing "." with "_",
    # so "candle_update" here must match exactly what
    # apps.market_data.signals.py sends.
    async def candle_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))
