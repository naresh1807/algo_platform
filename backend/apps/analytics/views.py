from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsTraderOrAdmin

from .models import PerformanceMetrics
from .serializers import PerformanceMetricsSerializer


class PerformanceMetricsViewSet(viewsets.ReadOnlyModelViewSet):
    # RBAC: was plain IsAuthenticated, inconsistent with the Trader/Admin
    # model common/permissions.py documents (any authenticated user, even
    # one in neither group, could read performance data). Aligned with
    # the rest of the "ordinary dashboard surface" -- Trader or Admin.
    queryset = PerformanceMetrics.objects.all()
    serializer_class = PerformanceMetricsSerializer
    permission_classes = [IsTraderOrAdmin]


class SharpeRatioView(APIView):
    """
    manual 11.14 lists "Sharpe Ratio (Future)" -- now real, computed
    from apps.risk.EquitySnapshot's actual equity curve (see
    apps.analytics.services.compute_sharpe_ratio). Not stored on
    PerformanceMetrics (a rolling statistic doesn't fit that per-day
    model) -- computed fresh on each call instead, same as any other
    on-demand analytics figure in this app.
    """
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from .services import compute_sharpe_ratio

        lookback_days = int(request.query_params.get("lookback_days", 30))
        sharpe = compute_sharpe_ratio(lookback_days=lookback_days)
        return Response({"lookback_days": lookback_days, "sharpe_ratio": sharpe})
