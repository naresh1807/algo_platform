from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BestStrikeView,
    EvaluateNowView,
    OptionChainSnapshotViewSet,
    OptionChainView,
    OptionContractViewSet,
    OptionExpiriesView,
    OptionsAnalyticsView,
    OptionsStrategySettingView,
)

router = DefaultRouter()
router.register("contracts", OptionContractViewSet, basename="option-contract")
router.register("snapshots", OptionChainSnapshotViewSet, basename="option-snapshot")

urlpatterns = router.urls + [
    path("analytics/", OptionsAnalyticsView.as_view(), name="options-analytics"),
    path("expiries/", OptionExpiriesView.as_view(), name="options-expiries"),
    path("chain/", OptionChainView.as_view(), name="options-chain"),
    path("best-strike/", BestStrikeView.as_view(), name="options-best-strike"),
    path("strategy-settings/", OptionsStrategySettingView.as_view(), name="options-strategy-settings"),
    path("evaluate/", EvaluateNowView.as_view(), name="options-evaluate-now"),
]
