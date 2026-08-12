from django.contrib import admin

from .models import OptionChainSnapshot, OptionContract


@admin.register(OptionContract)
class OptionContractAdmin(admin.ModelAdmin):
    """
    Mostly a read/inspect view now -- OptionContract rows are normally
    populated automatically (apps.options.tasks.sync_watchlist_option_contracts,
    weekly) or via `python manage.py sync_option_contracts` for a
    specific expiry. Still useful here for spot-checking what's synced,
    or manually adding a contract the automated sync hasn't reached yet.
    """
    list_display = ("underlying", "expiry", "strike", "option_type", "symbol_token")
    list_filter = ("underlying", "option_type")
    search_fields = ("symbol_token",)


@admin.register(OptionChainSnapshot)
class OptionChainSnapshotAdmin(admin.ModelAdmin):
    list_display = ("contract", "timestamp", "ltp", "open_interest", "change_in_oi", "iv")
    list_filter = ("timestamp",)
    readonly_fields = ("timestamp",)
