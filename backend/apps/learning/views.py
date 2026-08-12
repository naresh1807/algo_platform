from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_tools.audit import log_action
from common.permissions import IsAdminGroup, IsTraderOrAdmin

from .models import DailyReviewNote, DriftEvent, ModelRegistry, StrategyVersion, TradeReview
from .serializers import (
    DailyReviewNoteSerializer,
    DriftEventSerializer,
    ModelRegistrySerializer,
    StrategyVersionSerializer,
    TradeReviewSerializer,
)


class StrategyVersionViewSet(viewsets.ModelViewSet):
    """
    Full CRUD here (unlike most other viewsets) because promoting a new
    strategy version or flipping active_flag is a deliberate human
    action taken through this API/admin -- NOT something the daily
    learning job does automatically (manual section 21: "Never
    auto-change core safety rules" / "Never deploy unvalidated changes").

    RBAC (manual section 16): reading the list is fine for Trader or
    Admin, but creating/editing/promoting a version -- the actual
    governance action -- requires Admin specifically. get_permissions()
    branches on self.action rather than using one fixed
    permission_classes list, since "can view" and "can change" are
    deliberately different bars here.
    """
    queryset = StrategyVersion.objects.all()
    serializer_class = StrategyVersionSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsTraderOrAdmin()]
        return [IsAdminGroup()]

    def perform_update(self, serializer):
        was_active = serializer.instance.active_flag
        instance = serializer.save()
        if instance.active_flag and not was_active:
            log_action(
                action="strategy_version_promoted",
                actor=self.request.user,
                target=instance,
                details={"version_name": instance.version_name},
            )


class ModelRegistryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ModelRegistry.objects.all()
    serializer_class = ModelRegistrySerializer
    permission_classes = [IsAuthenticated]


class DailyReviewNoteViewSet(viewsets.ModelViewSet):
    """
    ModelViewSet (not read-only) specifically so a human can PATCH
    approved_flag=True from the dashboard -- that single field flip is
    the entire governance gate described in the manual's AI Deployment
    Backbone section. Same RBAC split as StrategyVersionViewSet: anyone
    with dashboard access can read reviews, only Admin can approve one.
    """
    queryset = DailyReviewNote.objects.all()
    serializer_class = DailyReviewNoteSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsTraderOrAdmin()]
        return [IsAdminGroup()]

    def perform_update(self, serializer):
        was_approved = serializer.instance.approved_flag
        instance = serializer.save()
        if instance.approved_flag and not was_approved:
            log_action(
                action="daily_review_approved",
                actor=self.request.user,
                target=instance,
                details={"review_date": str(instance.review_date)},
            )


class DriftEventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DriftEvent.objects.all()
    serializer_class = DriftEventSerializer
    permission_classes = [IsAuthenticated]


class TradeReviewViewSet(viewsets.ReadOnlyModelViewSet):
    """
    manual 13.8 Experience Memory / 11.10 Reward Engine -- read-only,
    same as DriftEvent/ModelRegistry: these are the learning loop's
    own generated records, not something a human edits through the API.
    """
    queryset = TradeReview.objects.select_related("position").all()
    serializer_class = TradeReviewSerializer
    permission_classes = [IsAuthenticated]


class TechnicalDirectionView(APIView):
    """
    Exposes apps.learning.ml_technical.predict_technical_direction --
    previously this model had no caller anywhere in the codebase outside
    its own module (trained manually via shell, never actually served).
    Purely advisory, same posture as every other ML signal in this
    platform: it does NOT gate or feed into apps.signals.engine's
    approve/reject decision, it's a second opinion a trader or JARVIS
    can read alongside the rule-based signal for the same symbol.
    """
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from apps.learning.ml_technical import predict_technical_direction

        symbol = request.query_params.get("symbol")
        timeframe = request.query_params.get("timeframe", "5m")
        if not symbol:
            return Response({"detail": "symbol query parameter is required."}, status=400)

        prediction = predict_technical_direction(symbol, timeframe)
        if prediction is None:
            return Response({
                "symbol": symbol, "timeframe": timeframe, "available": False,
                "detail": "No trained technical-direction model for this symbol/timeframe yet.",
            })
        return Response({"symbol": symbol, "timeframe": timeframe, "available": True, **prediction})
