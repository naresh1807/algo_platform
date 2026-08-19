from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from common.constants import SignalStatus
from common.permissions import IsAdminGroup, IsTraderOrAdmin

from apps.signals.models import TradingSignal

from .models import BrokerOrder, ExecutionModeSetting, OpenPosition
from .serializers import ExecutionModeSettingSerializer, OpenPositionSerializer


class OpenPositionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only: positions are opened/closed by the execution engine
    (apps/execution/broker.py, not yet written) after every one of the
    manual section 16 pre-trade checks passes -- never directly via API.
    """
    queryset = OpenPosition.objects.select_related("signal").all()
    serializer_class = OpenPositionSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["symbol", "side"]


# Typed into the frontend's Settings page before a switch to LIVE is
# accepted -- same "a deliberate, explicit human action" posture as
# apps.risk.management.commands.rearm_kill_switch's --confirm flag,
# adapted to a UI control instead of a CLI one since (unlike re-arming
# the kill switch) this needs to be reachable from the dashboard.
LIVE_MODE_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"


class ExecutionModeView(APIView):
    """
    GET/POST for apps.execution.models.ExecutionModeSetting -- the
    Settings page's Paper/Live trading toggle. Switching TO live
    requires the exact LIVE_MODE_CONFIRMATION_PHRASE in the request
    body; switching back to paper (the safer direction) never does,
    matching the general principle elsewhere in this codebase that
    de-risking an already-active setting should never have friction --
    only making things riskier should.

    Every change (either direction) is logged as a RiskEvent -- LIVE
    trading is real money, so exactly who flipped this and when must
    be reconstructable from the audit trail the same way every other
    consequential risk decision already is (see RiskEvent's own
    docstring). This does NOT flip the kill switch (RiskEvent creation
    alone never does -- only apps.risk.engine.trigger_kill_switch may,
    per its own docstring) -- it is an audit record, not a safety gate
    by itself.
    """

    permission_classes = [IsTraderOrAdmin]

    def get_permissions(self):
        # Reading the mode is harmless dashboard state; changing the global
        # real-money switch is a governance action and requires Admin RBAC.
        if self.request.method == "POST":
            return [IsAdminGroup()]
        return super().get_permissions()

    def get(self, request):
        row, _ = ExecutionModeSetting.objects.get_or_create(pk=1)
        return Response(ExecutionModeSettingSerializer(row).data)

    def post(self, request):
        mode = request.data.get("mode")
        if mode not in ExecutionModeSetting.Mode.values:
            return Response(
                {"error": f"mode must be one of {ExecutionModeSetting.Mode.values}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if mode == ExecutionModeSetting.Mode.LIVE:
            from django.conf import settings

            if not settings.LIVE_TRADING_ENABLED:
                return Response(
                    {
                        "error": (
                            "Live trading is disabled by server configuration. "
                            "Set LIVE_TRADING_ENABLED=1 only after deployment checks and broker reconciliation are ready."
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            if request.data.get("confirm_phrase") != LIVE_MODE_CONFIRMATION_PHRASE:
                return Response(
                    {
                        "error": (
                            f'Switching to live trading requires typing "{LIVE_MODE_CONFIRMATION_PHRASE}" '
                            "exactly -- this enables real order placement with real money."
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        with transaction.atomic():
            row, _ = ExecutionModeSetting.objects.select_for_update().get_or_create(pk=1)
            previous_mode = row.mode
            if previous_mode != mode:
                has_open_positions = OpenPosition.objects.filter(closed_at__isnull=True).exists()
                has_unresolved_orders = BrokerOrder.objects.filter(status__in=[
                    BrokerOrder.Status.CREATED,
                    BrokerOrder.Status.SUBMITTED,
                    BrokerOrder.Status.UNKNOWN,
                ]).exists()
                has_executing_signals = TradingSignal.objects.filter(
                    status=SignalStatus.EXECUTING,
                ).exists()
                if has_open_positions or has_unresolved_orders or has_executing_signals:
                    return Response(
                        {
                            "error": (
                                "Execution mode cannot change while positions, executing signals, "
                                "or unresolved broker orders exist."
                            )
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

                # Never carry an approval across venue boundaries. A signal
                # approved for paper must not become a real-money order merely
                # because the operator subsequently armed live mode.
                for stale in TradingSignal.objects.filter(status=SignalStatus.APPROVED):
                    stale.status = SignalStatus.EXPIRED
                    stale.reason += " [expired by execution-mode change]"
                    stale.save(update_fields=["status", "reason"])

                row.mode = mode
                row.changed_by = request.user.get_username()
                row.save()

                from common.constants import RiskEventSeverity

                from apps.risk.models import RiskEvent

                RiskEvent.objects.create(
                    event_type="execution_mode_changed",
                    message=(
                        f"Execution mode changed from {previous_mode.upper()} to {mode.upper()} "
                        f"by {row.changed_by}."
                    ),
                    severity=(
                        RiskEventSeverity.CRITICAL
                        if mode == ExecutionModeSetting.Mode.LIVE
                        else RiskEventSeverity.INFO
                    ),
                )

        return Response(ExecutionModeSettingSerializer(row).data)
