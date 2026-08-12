from django.apps import AppConfig


class InvestingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.investing"
    label = "investing"

    def ready(self):
        from . import signals  # noqa: F401
