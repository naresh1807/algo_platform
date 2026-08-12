from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import PerformanceMetricsViewSet, SharpeRatioView

router = DefaultRouter()
router.register("performance", PerformanceMetricsViewSet, basename="performance-metrics")

urlpatterns = router.urls + [
    path("sharpe-ratio/", SharpeRatioView.as_view(), name="sharpe-ratio"),
]
