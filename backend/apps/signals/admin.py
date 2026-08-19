from django.contrib import admin

from common.admin import ReadOnlyOperationalAdmin

from .models import TradingSignal


@admin.register(TradingSignal)
class TradingSignalAdmin(ReadOnlyOperationalAdmin):
    list_display = ("symbol", "signal_type", "status", "total_score", "regime", "created_at")
    list_filter = ("status", "signal_type", "regime")
    search_fields = ("symbol", "reason")
