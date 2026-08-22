from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    PaperAccountStatusView, PaperAIDecisionViewSet, PaperDailySummaryViewSet,
    PaperOrderViewSet, PaperPositionViewSet, PaperTradeViewSet,
)

app_name = "paper_trading"

router = DefaultRouter()
router.register("positions", PaperPositionViewSet, basename="paper-position")
router.register("orders", PaperOrderViewSet, basename="paper-order")
router.register("trades", PaperTradeViewSet, basename="paper-trade")
router.register("daily-summaries", PaperDailySummaryViewSet, basename="paper-daily-summary")
router.register("decisions", PaperAIDecisionViewSet, basename="paper-decision")

urlpatterns = router.urls + [
    path("account/", PaperAccountStatusView.as_view(), name="paper-account-status"),
]
