from django.apps import AppConfig


class JarvisConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.jarvis"
    label = "jarvis"
    verbose_name = "JARVIS AI Assistant"

    def ready(self):
        # Registers the post_save hooks in signals.py that turn events in
        # OTHER apps (a new TradingSignal, a kill-switch trip, a closed
        # position, a promoted model) into JARVIS notifications (manual
        # 14.16). Imported here, not at module load time, for the same
        # reason every other app in this codebase does this: model
        # classes must be fully loaded first.
        from . import signals  # noqa: F401
