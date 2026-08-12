from rest_framework.routers import DefaultRouter

from .views import OpenPositionViewSet

router = DefaultRouter()
router.register("positions", OpenPositionViewSet, basename="open-position")

urlpatterns = router.urls
