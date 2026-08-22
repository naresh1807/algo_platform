"""
Read-only monitoring API for the autonomous paper-trading dashboard.
No mutating endpoint exists anywhere in this module -- there is no
Buy/Sell/Modify/Cancel/Square-off action to expose, by construction
(spec: "Do not expose manual... controls as normal trading actions").
"""

from rest_framework import mixins, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsTraderOrAdmin

from .models import (
    PaperAccount, PaperAIDecision, PaperDailySummary, PaperOrder,
    PaperPosition, PaperSessionState, PaperTrade, get_or_create_account,
)
from .serializers import (
    PaperAccountSerializer, PaperAIDecisionSerializer, PaperDailySummarySerializer,
    PaperOrderSerializer, PaperPositionSerializer, PaperTradeSerializer,
)


class PaperAccountStatusView(APIView):
    """GET /api/paper-trading/account/ -- capital/P&L/drawdown snapshot
    plus the current FSM state and active policy model version."""

    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        account = get_or_create_account()
        session = PaperSessionState.objects.get_or_create(pk=1)[0]
        from apps.learning.models import ModelRegistry

        champion = ModelRegistry.objects.filter(model_name="paper_trading_policy", active_flag=True).first()
        data = PaperAccountSerializer(account).data
        data["session_state"] = session.state
        data["cooldown_until"] = session.cooldown_until
        data["champion_model_version"] = champion.model_version if champion else None
        data["champion_model_metrics"] = champion.metrics_json if champion else None
        return Response(data)


class PaperPositionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = PaperPosition.objects.all().select_related("contract")
    serializer_class = PaperPositionSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["status"]


class PaperOrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    # No hard [:N] slice here -- DjangoFilterBackend (this project's
    # DEFAULT_FILTER_BACKENDS) calls .filter() on this class attribute
    # when filterset_fields below match a query param, and Django raises
    # "Cannot filter a query once a slice has been taken" on an
    # already-sliced queryset. PaperOrder.Meta.ordering is already
    # "-requested_at", so DRF's own pagination (PAGE_SIZE=100) already
    # returns the most recent rows first without needing a manual cap.
    queryset = PaperOrder.objects.all().select_related("contract")
    serializer_class = PaperOrderSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["status", "purpose"]


class PaperTradeViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = PaperTrade.objects.all().select_related("position", "position__contract", "result")[:200]
    serializer_class = PaperTradeSerializer
    permission_classes = [IsTraderOrAdmin]


class PaperDailySummaryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = PaperDailySummary.objects.all()[:90]
    serializer_class = PaperDailySummarySerializer
    permission_classes = [IsTraderOrAdmin]


class PaperAIDecisionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    # Same reasoning as PaperOrderViewSet above: filterset_fields + a
    # sliced queryset is a live 500-on-filter bug, not just a style
    # nit -- PaperAIDecision.Meta.ordering ("-timestamp") already makes
    # the manual slice redundant with DRF's own pagination.
    queryset = PaperAIDecision.objects.all().select_related("contract")
    serializer_class = PaperAIDecisionSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["action", "is_hypothetical"]
