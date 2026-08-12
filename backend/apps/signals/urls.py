from rest_framework.routers import DefaultRouter

from .views import TradingSignalViewSet

router = DefaultRouter()
router.register("", TradingSignalViewSet, basename="trading-signal")

urlpatterns = router.urls
