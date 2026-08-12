from django.contrib import admin

from .models import OpenPosition


@admin.register(OpenPosition)
class OpenPositionAdmin(admin.ModelAdmin):
    list_display = ("symbol", "side", "qty", "entry_price", "unrealized_pnl", "opened_at", "closed_at")
    list_filter = ("side",)
    search_fields = ("symbol",)
