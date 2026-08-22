"""WebSocket route for the read-only paper-trading dashboard push."""

from django.urls import re_path

from .consumers import PaperTradingConsumer

websocket_urlpatterns = [
    re_path(r"ws/paper-trading/live/$", PaperTradingConsumer.as_asgi()),
]
