from django.urls import path
from rest_framework.routers import DefaultRouter

from .health import SystemHealthView
from .views import FeedHealthCheckViewSet, PriceAlertViewSet

app_name = "monitoring"

router = DefaultRouter()
router.register("feed-health", FeedHealthCheckViewSet, basename="feed-health")
router.register("price-alerts", PriceAlertViewSet, basename="price-alert")

urlpatterns = router.urls + [
    path("health/", SystemHealthView.as_view(), name="system-health"),
]
