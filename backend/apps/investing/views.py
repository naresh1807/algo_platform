from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsTraderOrAdmin

from .models import (
    FundamentalSnapshot,
    Index,
    IndexConstituent,
    IPOListing,
    Stock,
    StockRecommendation,
    StockWatchlist,
)
from .serializers import (
    FundamentalSnapshotSerializer,
    IndexConstituentSerializer,
    IndexSerializer,
    IPOListingSerializer,
    StockRecommendationSerializer,
    StockSerializer,
    StockWatchlistSerializer,
)


class StockViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Stock.objects.all()
    serializer_class = StockSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["sector", "exchange"]


class FundamentalSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = FundamentalSnapshot.objects.select_related("stock").all()
    serializer_class = FundamentalSnapshotSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["stock", "reporting_type"]


class StockWatchlistViewSet(viewsets.ModelViewSet):
    """Full CRUD, scoped to the requesting trader's own watchlist -- same pattern as apps.monitoring.PriceAlertViewSet."""
    serializer_class = StockWatchlistSerializer
    permission_classes = [IsTraderOrAdmin]

    def get_queryset(self):
        return StockWatchlist.objects.filter(user=self.request.user).select_related("stock")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class StockRecommendationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only -- these are apps.investing.tasks.generate_stock_recommendations'
    own output, same "the system generates it, the trader reads it"
    boundary as apps.learning.TradeReviewViewSet.
    """
    queryset = StockRecommendation.objects.select_related("stock").all()
    serializer_class = StockRecommendationSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["stock", "verdict"]

    def get_queryset(self):
        # Default to only the latest recommendation per stock unless a
        # specific stock's full history was asked for -- the endpoint
        # exists so the dashboard can show "current suggestions", not
        # every recommendation ever generated (that's `?stock=<id>` for
        # a single stock's own history, or the admin, if it's ever needed).
        if not self.request.query_params.get("stock"):
            from .sector_analysis import _latest_recommendations
            return _latest_recommendations()
        return super().get_queryset()


class IPOListingViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = IPOListing.objects.all()
    serializer_class = IPOListingSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["status"]


class IndexViewSet(viewsets.ReadOnlyModelViewSet):
    """
    The dashboard's ticker-card row (manual has nothing on this --
    trader-requested, broker-app-style index bar). `constituents/`
    below is the click-through to a single index's stock list.
    """
    queryset = Index.objects.all()
    serializer_class = IndexSerializer
    permission_classes = [IsTraderOrAdmin]


class IndexConstituentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Filter by ?index=<id> for one index's stock list -- the "click on
    NIFTY BANK, see its stocks" drill-down the trader asked for.
    """
    queryset = IndexConstituent.objects.select_related("stock", "index").all()
    serializer_class = IndexConstituentSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["index"]


class SectorBreakdownView(APIView):
    """
    manual has nothing on this -- trader-requested: "sector-wise
    verify + suggest the profitable stock there." See
    apps.investing.sector_analysis.sector_breakdown for the actual
    aggregation.
    """
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from .sector_analysis import sector_breakdown
        return Response(sector_breakdown())
