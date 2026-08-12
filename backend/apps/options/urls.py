from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BestStrikeView,
    OptionChainSnapshotViewSet,
    OptionChainView,
    OptionContractViewSet,
    OptionExpiriesView,
    OptionsAnalyticsView,
)

router = DefaultRouter()
router.register("contracts", OptionContractViewSet, basename="option-contract")
router.register("snapshots", OptionChainSnapshotViewSet, basename="option-snapshot")

urlpatterns = router.urls + [
    path("analytics/", OptionsAnalyticsView.as_view(), name="options-analytics"),
    path("expiries/", OptionExpiriesView.as_view(), name="options-expiries"),
    path("chain/", OptionChainView.as_view(), name="options-chain"),
    path("best-strike/", BestStrikeView.as_view(), name="options-best-strike"),
]
