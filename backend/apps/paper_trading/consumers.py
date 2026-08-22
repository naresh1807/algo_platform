"""
Read-only WebSocket push for the paper-trading dashboard -- same
AuthenticatedWebsocketConsumer/join_group_and_accept pattern
apps.risk.consumers.RiskAlertConsumer uses. No client->server message
handling of any kind (this connection only ever receives).
"""

import json

from common.websockets import AuthenticatedWebsocketConsumer

GROUP_NAME = "paper_trading_live"


class PaperTradingConsumer(AuthenticatedWebsocketConsumer):
    async def connect(self):
        await self.join_group_and_accept(GROUP_NAME)

    async def paper_trading_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))
