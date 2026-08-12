"""WebSocket routes for live index price pushes."""

from django.urls import re_path

from .consumers import LiveIndexConsumer

websocket_urlpatterns = [
    re_path(r"ws/investing/index-live/$", LiveIndexConsumer.as_asgi()),
]
