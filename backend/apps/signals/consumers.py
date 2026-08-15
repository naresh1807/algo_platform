"""
WebSocket consumer for live trading-signal pushes. Same pure-relay
design as apps.market_data.consumers.LiveCandleConsumer -- all signals
broadcast into one shared group, matching the frontend's fixed
/ws/signals/live/ endpoint.
"""

import json

from channels.generic.websocket import AsyncWebsocketConsumer

GROUP_NAME = "signals_live"


class LiveSignalConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

    async def signal_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))
