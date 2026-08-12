from django.apps import AppConfig


class OptionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.options"
    label = "options"

    def ready(self):
        # Registers apps/options/signals.py's @receiver so new
        # OptionChainSnapshot rows actually reach the WebSocket group --
        # same reasoning as apps.market_data.apps.MarketDataConfig.ready().
        from . import signals  # noqa: F401
