from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .engine import get_equity
from .models import KillSwitchState, RiskEvent
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
