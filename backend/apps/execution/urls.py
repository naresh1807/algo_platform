from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import ExecutionModeView, OpenPositionViewSet

router = DefaultRouter()
router.register("positions", OpenPositionViewSet, basename="open-position")

urlpatterns = router.urls + [
    path("mode/", ExecutionModeView.as_view(), name="execution-mode"),
]
