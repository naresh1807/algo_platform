from django.contrib import admin

from .models import JarvisCommandHistory, JarvisMemory, MarketOutlookSnapshot


@admin.register(JarvisCommandHistory)
class JarvisCommandHistoryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "raw_text", "intent_action", "success", "needs_confirmation", "execution_ms")
    list_filter = ("intent_category", "success", "needs_confirmation", "input_mode")
    search_fields = ("raw_text", "result_text")
    readonly_fields = [f.name for f in JarvisCommandHistory._meta.fields]

    def has_add_permission(self, request):
        # Command history is a machine-written audit trail, same
        # rationale as apps.admin_tools.AuditLog -- not something to be
        # hand-edited from the admin.
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(JarvisMemory)
class JarvisMemoryAdmin(admin.ModelAdmin):
    list_display = ("user", "updated_at")


@admin.register(MarketOutlookSnapshot)
class MarketOutlookSnapshotAdmin(admin.ModelAdmin):
    list_display = ("generated_at", "bias", "bias_confidence", "best_options_idea", "best_stock_idea")
    list_filter = ("bias",)
    readonly_fields = [f.name for f in MarketOutlookSnapshot._meta.fields]

    def has_add_permission(self, request):
        return False  # machine-generated only, same as JarvisCommandHistory above

    def has_change_permission(self, request, obj=None):
        return False
