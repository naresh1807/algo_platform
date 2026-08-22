from django.apps import AppConfig


class PaperTradingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.paper_trading"
    label = "paper_trading"

    def ready(self):
        # See services/angel_one_guard.py's own docstring: importing it
        # here (not lazily inside a request/task) makes the structural
        # "this app never imports apps.market_data.broker_client"
        # guarantee fail LOUDLY at process startup if it's ever violated,
        # rather than silently at whatever moment the violating code path
        # first runs.
        from .services import angel_one_guard

        angel_one_guard.assert_no_broker_import()

        # Registers signals.py's @receiver so PaperAccount/PaperPosition/
        # PaperAIDecision changes actually reach the "paper_trading_live"
        # WebSocket group -- same pattern apps.risk/apps.options use.
        from . import signals  # noqa: F401
