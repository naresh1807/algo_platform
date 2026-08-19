from rest_framework.routers import DefaultRouter

from .views import TradingSignalViewSet

app_name = "signals"

router = DefaultRouter()
router.register("", TradingSignalViewSet, basename="trading-signal")

urlpatterns = router.urls
