"""
ASGI entrypoint. Routes HTTP to Django as usual, and WebSocket connections
to the Channels routing defined per-app (each real-time-relevant app -- e.g.
market_data for live candles, signals for live signal pushes, risk for
kill-switch/alerts -- exposes its own `routing.py` with a list of
websocket_urlpatterns; they're combined here rather than in one giant
global file so each app owns its own real-time surface).
"""

import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_asgi_app = get_asgi_application()

# Imported after get_asgi_application() so app registry is ready before
# any app's routing.py imports models/consumers.
from apps.market_data.routing import websocket_urlpatterns as market_ws  # noqa: E402
from apps.signals.routing import websocket_urlpatterns as signals_ws  # noqa: E402
from apps.risk.routing import websocket_urlpatterns as risk_ws  # noqa: E402
from apps.options.routing import websocket_urlpatterns as options_ws  # noqa: E402
from apps.jarvis.routing import websocket_urlpatterns as jarvis_ws  # noqa: E402
from apps.investing.routing import websocket_urlpatterns as investing_ws  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(
            URLRouter(market_ws + signals_ws + risk_ws + options_ws + jarvis_ws + investing_ws)
        ),
    }
)
