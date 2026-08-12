"""WebSocket routes for JARVIS push notifications."""

from django.urls import re_path

from .consumers import JarvisNotificationConsumer

websocket_urlpatterns = [
    re_path(r"ws/jarvis/live/$", JarvisNotificationConsumer.as_asgi()),
]
