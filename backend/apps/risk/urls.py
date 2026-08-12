from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import EquityStatusView, KillSwitchStatusView, RiskEventViewSet

router = DefaultRouter()
router.register("events", RiskEventViewSet, basename="risk-event")

urlpatterns = router.urls + [
    path("kill-switch/", KillSwitchStatusView.as_view(), name="kill-switch-status"),
    path("equity/", EquityStatusView.as_view(), name="equity-status"),
]
