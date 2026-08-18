from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .engine import get_equity
from .models import EquitySnapshot, KillSwitchState, RiskEvent
from .serializers import AccountEquitySerializer, KillSwitchStateSerializer, RiskEventSerializer


class RiskEventViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Read-only log viewer. Rows are written by the risk engine, not via the API."""
    queryset = RiskEvent.objects.all()
    serializer_class = RiskEventSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["severity", "event_type"]


class KillSwitchStatusView(APIView):
    """
    GET returns current kill-switch state for the dashboard.
    Deliberately NOT exposing a way to *activate* the kill switch via
    the API in this scaffold -- activation should only ever happen from
    inside the risk engine itself (a hard limit breach), never from an
    external call. Deactivation (re-arming) is a separate, deliberately
    manual-only management command, not an API endpoint, per the
    manual's safety rules.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        state, _ = KillSwitchState.objects.get_or_create(pk=1)
        return Response(KillSwitchStateSerializer(state).data)


class EquityStatusView(APIView):
    """GET current equity/drawdown/consecutive-losses for the dashboard's risk panel."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        equity = get_equity()
        data = AccountEquitySerializer(equity).data
        data["drawdown_pct"] = equity.drawdown_pct
        data["daily_pnl_pct"] = equity.daily_pnl_pct
        return Response(data)


class EquityCurveView(APIView):
    """
    GET the raw EquitySnapshot time series over a trailing window -- the
    same append-only history apps.analytics.services' Sharpe/Sortino/
    Calmar functions already read, exposed directly here so the
    performance dashboard can plot an actual equity curve instead of only
    showing the derived ratios. Read-only, ascending by time (chart
    libraries expect strictly increasing x-values).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone

        lookback_days = int(request.query_params.get("lookback_days", 30))
        window_start = timezone.now() - timedelta(days=lookback_days)
        snapshots = EquitySnapshot.objects.filter(timestamp__gte=window_start).order_by("timestamp")
        return Response({
            "lookback_days": lookback_days,
            "results": [{"timestamp": s.timestamp, "equity": s.equity} for s in snapshots],
        })
