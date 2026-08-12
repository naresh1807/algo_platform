from django.contrib import admin

from .models import TradingSignal


@admin.register(TradingSignal)
class TradingSignalAdmin(admin.ModelAdmin):
    list_display = ("symbol", "signal_type", "status", "total_score", "regime", "created_at")
    list_filter = ("status", "signal_type", "regime")
    search_fields = ("symbol", "reason")
    readonly_fields = ("created_at",)
