from django.contrib import admin

from .models import ExecutionModeSetting, OpenPosition


@admin.register(OpenPosition)
class OpenPositionAdmin(admin.ModelAdmin):
    list_display = ("symbol", "side", "qty", "entry_price", "unrealized_pnl", "opened_at", "closed_at")
    list_filter = ("side",)
    search_fields = ("symbol",)


@admin.register(ExecutionModeSetting)
class ExecutionModeSettingAdmin(admin.ModelAdmin):
    list_display = ("mode", "changed_by", "changed_at")
