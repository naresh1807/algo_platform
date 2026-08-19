from datetime import date

from django.utils import timezone as django_timezone
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsTraderOrAdmin

from .impact_engine import generate_market_summary
from .models import NewsImpactAnalysis, NewsSentiment
from .serializers import NewsImpactAnalysisSerializer, NewsSentimentSerializer


class NewsSentimentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = NewsSentiment.objects.select_related("impact_analysis").all()
    serializer_class = NewsSentimentSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["symbol", "sentiment_label"]


class NewsImpactAnalysisViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = NewsImpactAnalysis.objects.select_related("news").all()
    serializer_class = NewsImpactAnalysisSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["trade_relevance", "time_horizon"]


class MarketSummaryView(APIView):
    """
    manual section 10, pipeline step 7: "Generate Indian market
    summary." Thin wrapper over impact_engine.generate_market_summary --
    see that function's docstring for why it's a templated rollup of
    already-computed structured data, not another LLM call.
    """
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        date_str = request.query_params.get("date")
        # localdate(), not date.today() -- see apps/market_data/broker_client.py's
        # note on why a host-system-timezone-dependent "today" is a real
        # bug risk, not just a style nitpick.
        try:
            for_date = date.fromisoformat(date_str) if date_str else django_timezone.localdate()
        except ValueError:
            return Response({"detail": "date must use YYYY-MM-DD format."}, status=400)
        return Response({"date": for_date.isoformat(), "summary": generate_market_summary(for_date)})
