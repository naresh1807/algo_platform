from django.apps import AppConfig


class LearningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.learning"
    label = "learning"

    def ready(self):
        # Registers the post_save hook in signals.py that creates a
        # TradeReview (manual 13.8 Experience Memory) whenever a
        # position closes. Imported here, not at module load time, for
        # the same reason every other app in this codebase does this:
        # model classes must be fully loaded first.
        from . import signals  # noqa: F401
