from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "actor_display", "target_type", "target_id", "created_at")
    list_filter = ("action", "target_type")
    search_fields = ("actor_label", "target_type", "target_id")
    readonly_fields = [f.name for f in AuditLog._meta.fields]  # audit log entries are never hand-edited

    def actor_display(self, obj):
        return obj.actor.username if obj.actor else (obj.actor_label or "—")
    actor_display.short_description = "Actor"

    def has_add_permission(self, request):
        return False  # entries are only ever created via log_action(), never manually

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
