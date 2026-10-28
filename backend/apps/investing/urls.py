from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    FundamentalSnapshotViewSet,
    IndexConstituentViewSet,
    IndexViewSet,
    IPOListingViewSet,
    SectorBreakdownView,
    StockRecommendationViewSet,
    StockViewSet,
    StockWatchlistViewSet,
)

from .delivery_views import DeliveryStocksView, DeliveryAccountView, DeliveryOrdersView

from .btst_views import BTSTView

app_name = "investing"

router = DefaultRouter()
router.register("stocks", StockViewSet, basename="stock")
router.register("fundamentals", FundamentalSnapshotViewSet, basename="fundamental-snapshot")
router.register("watchlist", StockWatchlistViewSet, basename="stock-watchlist")
router.register("recommendations", StockRecommendationViewSet, basename="stock-recommendation")
router.register("ipos", IPOListingViewSet, basename="ipo-listing")
router.register("indices", IndexViewSet, basename="index")
router.register("index-constituents", IndexConstituentViewSet, basename="index-constituent")

urlpatterns = router.urls + [
    path("btst/", BTSTView.as_view()),
    path("delivery/stocks/", DeliveryStocksView.as_view()),
    path("delivery/account/", DeliveryAccountView.as_view()),
    path("delivery/orders/", DeliveryOrdersView.as_view()),
    path("sector-breakdown/", SectorBreakdownView.as_view(), name="sector-breakdown"),
]
