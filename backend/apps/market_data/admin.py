from django.contrib import admin

from .models import HistoricalData


@admin.register(HistoricalData)
class HistoricalDataAdmin(admin.ModelAdmin):
    list_display = ("symbol", "timeframe", "timestamp", "close", "volume", "source")
    list_filter = ("timeframe", "source")
    search_fields = ("symbol",)
