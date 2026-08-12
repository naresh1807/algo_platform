from django.contrib import admin

from .models import DailyReviewNote, DriftEvent, ModelRegistry, StrategyVersion, TradeReview


@admin.register(StrategyVersion)
class StrategyVersionAdmin(admin.ModelAdmin):
    list_display = ("version_name", "active_flag", "created_at")
    list_filter = ("active_flag",)


@admin.register(ModelRegistry)
class ModelRegistryAdmin(admin.ModelAdmin):
    list_display = ("model_name", "model_version", "active_flag", "created_at")
    list_filter = ("active_flag", "model_name")


@admin.register(DailyReviewNote)
class DailyReviewNoteAdmin(admin.ModelAdmin):
    list_display = ("review_date", "approved_flag")
    list_filter = ("approved_flag",)


@admin.register(DriftEvent)
class DriftEventAdmin(admin.ModelAdmin):
    list_display = ("drift_type", "metric_name", "severity", "detected_at", "action_taken")
    list_filter = ("drift_type", "severity")


@admin.register(TradeReview)
class TradeReviewAdmin(admin.ModelAdmin):
    list_display = ("position", "reward_score", "r_multiple", "confidence_band", "created_at")
    list_filter = ("confidence_band",)
