from django.contrib import admin

from common.admin import ReadOnlyOperationalAdmin

from .models import AccountEquity, KillSwitchState, RiskEvent


@admin.register(RiskEvent)
class RiskEventAdmin(ReadOnlyOperationalAdmin):
    list_display = ("event_type", "severity", "symbol", "created_at")
    list_filter = ("severity", "event_type")
    search_fields = ("symbol", "message")


@admin.register(KillSwitchState)
class KillSwitchStateAdmin(ReadOnlyOperationalAdmin):
    list_display = ("is_active", "activated_at", "deactivated_at", "deactivated_by")


@admin.register(AccountEquity)
class AccountEquityAdmin(ReadOnlyOperationalAdmin):
    list_display = ("current_equity", "peak_equity", "drawdown_pct", "consecutive_losses", "trading_day")
