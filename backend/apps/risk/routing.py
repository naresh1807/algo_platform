"""WebSocket routes for kill-switch/risk-alert pushes."""

from django.urls import re_path

from .consumers import RiskAlertConsumer

websocket_urlpatterns = [
    re_path(r"ws/risk/live/$", RiskAlertConsumer.as_asgi()),
]
