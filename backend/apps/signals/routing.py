"""WebSocket routes for live signal pushes."""

from django.urls import re_path

from .consumers import LiveSignalConsumer

websocket_urlpatterns = [
    re_path(r"ws/signals/live/$", LiveSignalConsumer.as_asgi()),
]
