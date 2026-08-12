"""
WebSocket routes for live candle pushes.
"""

from django.urls import re_path

from .consumers import LiveCandleConsumer

websocket_urlpatterns = [
    re_path(r"ws/market-data/live/$", LiveCandleConsumer.as_asgi()),
]
