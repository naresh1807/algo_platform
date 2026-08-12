from django.apps import AppConfig


class MarketDataConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.market_data"
    label = "market_data"

    def ready(self):
        # Importing this module registers its @receiver-decorated
        # functions with Django's signal dispatcher -- without this
        # import happening somewhere, apps/market_data/signals.py's
        # broadcast_new_candle would never actually connect and new
        # candles would silently never reach the WebSocket group.
        from . import signals  # noqa: F401
