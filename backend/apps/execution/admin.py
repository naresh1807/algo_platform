from django.contrib import admin

from common.admin import ReadOnlyOperationalAdmin

from .models import BrokerOrder, ExecutionModeSetting, OpenPosition


@admin.register(OpenPosition)
class OpenPositionAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        "symbol", "execution_mode", "timeframe", "side", "qty", "entry_price",
        "unrealized_pnl", "gross_realized_pnl", "total_costs", "realized_pnl",
        "opened_at", "closed_at",
    )
    list_filter = ("execution_mode", "timeframe", "side")
    search_fields = ("symbol",)


@admin.register(ExecutionModeSetting)
class ExecutionModeSettingAdmin(admin.ModelAdmin):
    list_display = ("mode", "changed_by", "changed_at")
    readonly_fields = ("mode", "changed_by", "changed_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BrokerOrder)
class BrokerOrderAdmin(admin.ModelAdmin):
    """Read-only incident view for durable live-order reconciliation."""

    list_display = (
        "id", "purpose", "symbol", "side", "quantity", "status",
        "broker_order_id", "created_at", "updated_at",
    )
    list_filter = ("purpose", "status", "side")
    search_fields = ("symbol", "broker_order_id", "idempotency_key")
    readonly_fields = tuple(field.name for field in BrokerOrder._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
