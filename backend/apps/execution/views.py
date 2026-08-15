from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ExecutionModeSetting, OpenPosition
from .serializers import ExecutionModeSettingSerializer, OpenPositionSerializer


class OpenPositionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only: positions are opened/closed by the execution engine
    (apps/execution/broker.py, not yet written) after every one of the
    manual section 16 pre-trade checks passes -- never directly via API.
    """
    queryset = OpenPosition.objects.select_related("signal").all()
    serializer_class = OpenPositionSerializer
    permission_classes = [IsAuthenticated]
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

    permission_classes = [IsAuthenticated]

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

        row, _ = ExecutionModeSetting.objects.get_or_create(pk=1)
        previous_mode = row.mode
        row.mode = mode
        row.changed_by = request.user.get_username()
        row.save()

        if previous_mode != mode:
            from common.constants import RiskEventSeverity

            from apps.risk.models import RiskEvent

            RiskEvent.objects.create(
                event_type="execution_mode_changed",
                message=(
                    f"Execution mode changed from {previous_mode.upper()} to {mode.upper()} "
                    f"by {row.changed_by}."
                ),
                severity=RiskEventSeverity.CRITICAL if mode == ExecutionModeSetting.Mode.LIVE else RiskEventSeverity.INFO,
            )

        return Response(ExecutionModeSettingSerializer(row).data)
