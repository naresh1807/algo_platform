from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    DailyPnLReportView, PerformanceBreakdownView, PerformanceMetricsViewSet, SharpeRatioView,
)

router = DefaultRouter()
router.register("performance", PerformanceMetricsViewSet, basename="performance-metrics")

urlpatterns = router.urls + [
    path("sharpe-ratio/", SharpeRatioView.as_view(), name="sharpe-ratio"),
    path("daily-pnl/", DailyPnLReportView.as_view(), name="daily-pnl-report"),
    path("performance-breakdown/", PerformanceBreakdownView.as_view(), name="performance-breakdown"),
]
