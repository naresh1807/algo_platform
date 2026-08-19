"""
WebSocket consumer for JARVIS push notifications (manual 14.16:
"JARVIS Announces" -- market open/close, trade executed, signal
generated, risk warning, kill switch, AI training completed). Same
group-broadcast pattern as apps.risk.consumers.RiskAlertConsumer and
apps.market_data.consumers -- one group, JSON payloads tagged by
"type" so the frontend can branch.

This is push-only (server -> client announcements); the actual command
request/response loop goes through the REST endpoint
(JarvisCommandView), same division of labor as everywhere else in this
codebase (REST for request/response, WebSocket for server-initiated
push).
"""

import json

from common.websockets import AuthenticatedWebsocketConsumer

GROUP_NAME = "jarvis_live"


class JarvisNotificationConsumer(AuthenticatedWebsocketConsumer):
    async def connect(self):
        await self.join_group_and_accept(GROUP_NAME)

    async def jarvis_announcement(self, event):
        await self.send(text_data=json.dumps(event["data"]))
