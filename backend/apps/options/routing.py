"""WebSocket routes for live option-chain snapshot pushes."""

from django.urls import re_path

from .consumers import LiveOptionChainConsumer

websocket_urlpatterns = [
    re_path(r"ws/options/live/$", LiveOptionChainConsumer.as_asgi()),
]
