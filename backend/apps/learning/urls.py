from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    DailyReviewNoteViewSet,
    DriftEventViewSet,
    ModelRegistryViewSet,
    StrategyVersionViewSet,
    TechnicalDirectionView,
    TradeReviewViewSet,
)

router = DefaultRouter()
router.register("strategy-versions", StrategyVersionViewSet, basename="strategy-version")
router.register("model-registry", ModelRegistryViewSet, basename="model-registry")
router.register("daily-reviews", DailyReviewNoteViewSet, basename="daily-review")
router.register("drift-events", DriftEventViewSet, basename="drift-event")
router.register("trade-reviews", TradeReviewViewSet, basename="trade-review")

urlpatterns = [
    path("technical-direction/", TechnicalDirectionView.as_view(), name="technical-direction"),
] + router.urls
