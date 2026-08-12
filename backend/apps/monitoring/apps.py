from django.apps import AppConfig


class MonitoringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.monitoring"
    label = "monitoring"

    def ready(self):
        from . import signals as ws_signals  # noqa: F401
