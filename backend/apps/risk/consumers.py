"""
WebSocket consumer for kill-switch state changes and critical risk
alerts. Matches the frontend's fixed /ws/risk/live/ endpoint and its
handleRiskMessage() in liveStore.js, which branches on msg.type
("kill_switch", "feed_health", or anything else treated as a generic
risk alert) -- both message shapes broadcast from apps.risk.signals
must keep using those exact "type" values.
"""

import json

from common.websockets import AuthenticatedWebsocketConsumer

GROUP_NAME = "risk_live"


class RiskAlertConsumer(AuthenticatedWebsocketConsumer):
    async def connect(self):
        await self.join_group_and_accept(GROUP_NAME)

    async def risk_alert(self, event):
        await self.send(text_data=json.dumps(event["data"]))
