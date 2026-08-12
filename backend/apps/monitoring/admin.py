from django.contrib import admin

from .models import FeedHealthCheck, PriceAlert


@admin.register(FeedHealthCheck)
class FeedHealthCheckAdmin(admin.ModelAdmin):
    list_display = ("source", "is_healthy", "latency_ms", "checked_at")
    list_filter = ("source", "is_healthy")


@admin.register(PriceAlert)
class PriceAlertAdmin(admin.ModelAdmin):
    list_display = ("symbol", "condition", "target_price", "active", "triggered_at")
    list_filter = ("symbol", "condition", "active")
