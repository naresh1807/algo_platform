from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .indicators import compute_indicator_series
from .models import HistoricalData
from .serializers import HistoricalDataSerializer


class CandlePagination(PageNumberPagination):
    """
    Chart data needs far more than the project-wide default PAGE_SIZE=100
    (that default is right for lists like RiskEvent/TradingSignal, wrong
    for a price chart -- 100 candles on a 5m timeframe is under 8 hours
    of a single trading day). Default here is 1000, and the frontend can
    request more with ?page_size= up to max_page_size -- without this,
    a symbol with thousands of ingested candles would only ever show its
    most recent ~100 on the chart with no visible indication that older
    data existed but was silently cut off by pagination.
    """

    page_size = 1000
    page_size_query_param = "page_size"
    max_page_size = 10000


class HistoricalDataViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only: candle data is written by the ingestion job (Celery task,
    not yet implemented), never via the API -- the API is for the
    dashboard/chart to read from.
    """
    queryset = HistoricalData.objects.all()
    serializer_class = HistoricalDataSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["symbol", "timeframe"]
    pagination_class = CandlePagination


class IndicatorSeriesView(APIView):
    """
    Serves the same EMA9/EMA21/SAR/RSI/MACD values apps.signals.engine
    already computes off of HistoricalData, but as a full per-candle
    series rather than just the latest snapshot -- so the dashboard
    chart can plot them instead of showing bare candles. Read-only,
    same auth as candles/: this endpoint computes from HistoricalData,
    it never writes anything.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        symbol = request.query_params.get("symbol")
        timeframe = request.query_params.get("timeframe")
        if not symbol or not timeframe:
            return Response(
                {"detail": "symbol and timeframe query params are required."}, status=400,
            )

        limit = int(request.query_params.get("limit", 300))
        series = compute_indicator_series(symbol, timeframe, limit=limit)
        return Response({"results": series})
