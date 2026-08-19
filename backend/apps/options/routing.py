"""WebSocket routes for live option-chain snapshot pushes and per-contract candle streams."""

from django.urls import re_path

from .consumers import LiveOptionChainConsumer, OptionCandleConsumer

websocket_urlpatterns = [
    re_path(r"ws/options/live/$", LiveOptionChainConsumer.as_asgi()),
    re_path(
        r"ws/options/candles/(?P<contract_id>\d+)/(?P<timeframe>\w+)/$",
        OptionCandleConsumer.as_asgi(),
    ),
]
