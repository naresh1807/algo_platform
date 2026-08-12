from django.contrib import admin

from .models import PerformanceMetrics


@admin.register(PerformanceMetrics)
class PerformanceMetricsAdmin(admin.ModelAdmin):
    list_display = ("date", "total_trades", "win_rate", "profit_factor", "max_drawdown")
