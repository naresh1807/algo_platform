from rest_framework import mixins, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView
import logging

from apps.admin_tools.audit import log_action
from common.constants import RiskEventSeverity
from common.permissions import IsTraderOrAdmin

from .engine import get_equity, trigger_kill_switch
from .models import EquitySnapshot, KillSwitchState, RiskEvent
from .serializers import AccountEquitySerializer, KillSwitchStateSerializer, RiskEventSerializer

logger = logging.getLogger(__name__)


class RiskEventViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Read-only log viewer. Rows are written by the risk engine, not via the API."""
    queryset = RiskEvent.objects.all()
    serializer_class = RiskEventSerializer
    permission_classes = [IsTraderOrAdmin]
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
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        state, _ = KillSwitchState.objects.get_or_create(pk=1)
        return Response(KillSwitchStateSerializer(state).data)

    def post(self, request):
        """Activate the emergency stop; re-arming remains CLI-only."""
        if not IsTraderOrAdmin().has_permission(request, self):
            return Response(
                {"detail": "This action requires Trader or Admin privileges."},
                status=status.HTTP_403_FORBIDDEN,
            )
        state, _ = KillSwitchState.objects.get_or_create(pk=1)
        if state.is_active:
            self._dispatch_flatten()
            return Response(KillSwitchStateSerializer(state).data)
        reason = str(request.data.get("reason") or "Manual emergency stop").strip()
        event = RiskEvent.objects.create(
            event_type="manual_kill_switch",
            message=f"Kill switch manually activated by {request.user.get_username()}: {reason}",
            severity=RiskEventSeverity.CRITICAL,
        )
        trigger_kill_switch("manual_emergency_stop", event)
        state.refresh_from_db()
        log_action(
            action="kill_switch_activated",
            actor=request.user,
            target=state,
            details={"reason": reason},
        )
        self._dispatch_flatten()
        return Response(KillSwitchStateSerializer(state).data, status=status.HTTP_200_OK)

    @staticmethod
    def _dispatch_flatten() -> None:
        """Best-effort immediate dispatch; the periodic cycle remains a retry."""
        try:
            from apps.execution.tasks import flatten_open_positions

            flatten_open_positions.apply_async(queue="priority")
        except Exception:
            # The kill state is the source of truth and must stay active even
            # if Redis/Celery is temporarily unavailable.
            logger.exception("Unable to enqueue the immediate emergency flatten task")


class EquityStatusView(APIView):
    """GET current equity/drawdown/consecutive-losses for the dashboard's risk panel."""

    permission_classes = [IsTraderOrAdmin]

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
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone

        try:
            lookback_days = int(request.query_params.get("lookback_days", 30))
        except (TypeError, ValueError):
            return Response(
                {"error": "lookback_days must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not 1 <= lookback_days <= 3650:
            return Response(
                {"error": "lookback_days must be between 1 and 3650."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        window_start = timezone.now() - timedelta(days=lookback_days)
        snapshots = EquitySnapshot.objects.filter(timestamp__gte=window_start).order_by("timestamp")
        return Response({
            "lookback_days": lookback_days,
            "results": [{"timestamp": s.timestamp, "equity": s.equity} for s in snapshots],
        })
