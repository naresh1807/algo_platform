"""
WebSocket consumer for live trading-signal pushes. Same pure-relay
design as apps.market_data.consumers.LiveCandleConsumer -- all signals
broadcast into one shared group, matching the frontend's fixed
/ws/signals/live/ endpoint.
"""

import json

from common.websockets import AuthenticatedWebsocketConsumer

GROUP_NAME = "signals_live"


class LiveSignalConsumer(AuthenticatedWebsocketConsumer):
    async def connect(self):
        await self.join_group_and_accept(GROUP_NAME)

    async def signal_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))
