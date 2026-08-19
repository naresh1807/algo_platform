from rest_framework.routers import DefaultRouter

from .views import FeedHealthCheckViewSet, PriceAlertViewSet

app_name = "monitoring"

router = DefaultRouter()
router.register("feed-health", FeedHealthCheckViewSet, basename="feed-health")
router.register("price-alerts", PriceAlertViewSet, basename="price-alert")

urlpatterns = router.urls
