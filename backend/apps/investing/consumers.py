"""
WebSocket consumer for live index/constituent price pushes -- same
pure-relay shape as apps/options/consumers.py and apps/market_data/
consumers.py. All indices share one group ("index_live"); the
frontend (IndexTickerBar.jsx) filters by index id client-side, same
reasoning apps.options.consumers gives for sharing one group across
underlyings/expiries.

IMPORTANT scope note -- read before assuming this delivers true
per-second ticks: this consumer pushes whatever apps.investing.signals
broadcasts, which fires whenever an IndexPriceSnapshot/IndexConstituent
row is SAVED. Right now that's apps.investing.tasks.
sync_index_constituents_and_prices, on its REST-poll Celery schedule
(every few minutes -- see config/celery.py). Pushing over a WebSocket
the instant new data is saved is a real improvement over the frontend
having to poll REST itself, but it does NOT change how often NEW data
actually arrives -- that's still bounded by the REST poll interval,
because NSE/BSE's public APIs used here are polled endpoints, not a
streaming feed. See apps/investing/live_feed.py for what genuine
per-second ticks would actually require.
"""

import json

from channels.generic.websocket import AsyncWebsocketConsumer

GROUP_NAME = "index_live"


class LiveIndexConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

    # Called by group_send({"type": "index_update", ...}) -- must match
    # apps/investing/signals.py's group_send call exactly.
    async def index_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))
