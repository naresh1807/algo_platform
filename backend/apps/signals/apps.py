from django.apps import AppConfig


class SignalsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.signals"
    label = "signals"

    def ready(self):
        # Aliased on import only to avoid the visual confusion of
        # "from . import signals" inside the apps.signals package
        # itself -- this apps.signals.signals module is Django's
        # signal-dispatch pattern (post_save receivers), an unrelated
        # naming collision with the "trading signal" domain concept
        # this whole app is about.
        from . import signals as ws_signals  # noqa: F401
