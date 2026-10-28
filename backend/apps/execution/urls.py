from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import ExecutionModeView, OpenPositionViewSet
from .option_views import OptionOrdersView, OptionPreviewView

app_name = "execution"

router = DefaultRouter()
router.register("positions", OpenPositionViewSet, basename="open-position")

urlpatterns = router.urls + [
    path("option-preview/", OptionPreviewView.as_view(), name="option-preview"),
    path("option-orders/", OptionOrdersView.as_view(), name="option-orders"),
    path("mode/", ExecutionModeView.as_view(), name="execution-mode"),
]
