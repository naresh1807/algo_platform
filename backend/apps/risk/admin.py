from django.contrib import admin

from .models import AccountEquity, KillSwitchState, RiskEvent


@admin.register(RiskEvent)
class RiskEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "severity", "symbol", "created_at")
    list_filter = ("severity", "event_type")
    search_fields = ("symbol", "message")


@admin.register(KillSwitchState)
class KillSwitchStateAdmin(admin.ModelAdmin):
    list_display = ("is_active", "activated_at", "deactivated_at", "deactivated_by")


@admin.register(AccountEquity)
class AccountEquityAdmin(admin.ModelAdmin):
    list_display = ("current_equity", "peak_equity", "drawdown_pct", "consecutive_losses", "trading_day")
    readonly_fields = ("updated_at",)
