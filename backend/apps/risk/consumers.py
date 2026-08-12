"""
WebSocket consumer for kill-switch state changes and critical risk
alerts. Matches the frontend's fixed /ws/risk/live/ endpoint and its
handleRiskMessage() in liveStore.js, which branches on msg.type
("kill_switch", "feed_health", or anything else treated as a generic
risk alert) -- both message shapes broadcast from apps.risk.signals
must keep using those exact "type" values.
"""

import json

from channels.generic.websocket import AsyncWebsocketConsumer

GROUP_NAME = "risk_live"


class RiskAlertConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

    async def risk_alert(self, event):
        await self.send(text_data=json.dumps(event["data"]))
