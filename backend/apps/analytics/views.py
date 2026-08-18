from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsTraderOrAdmin

from .models import PerformanceMetrics
from .serializers import PerformanceMetricsSerializer


class DailyPnLReportView(APIView):
    """
    manual section 18's daily P&L report. PerformanceMetrics rows for
    PAST days are already stored (computed once by the nightly
    apps.learning.tasks.run_daily_review -> compute_daily_performance,
    per that model's own "backward-looking, never recomputed" docstring)
    -- read straight from the table for those. TODAY is the one
    exception: its trading isn't over yet and the nightly job hasn't run
    for it, so its row is refreshed live on every request via
    compute_daily_performance before reading the window back out, same
    "always read a stored row, but keep today's genuinely current"
    approach apps.risk.engine takes for AccountEquity vs. this app's own
    backward-looking metrics.
    """
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from datetime import timedelta

        from django.db.models import Sum
        from django.utils import timezone

        from .services import compute_daily_performance

        days = int(request.query_params.get("days", 30))
        today = timezone.localdate()
        compute_daily_performance(today)

        window_start = today - timedelta(days=days - 1)
        rows = PerformanceMetrics.objects.filter(
            date__gte=window_start, date__lte=today,
        ).order_by("-date")

        totals = rows.aggregate(total_net_pnl=Sum("net_pnl"), total_trades=Sum("total_trades"))

        return Response({
            "days": days,
            "results": PerformanceMetricsSerializer(rows, many=True).data,
            "summary": {
                "total_net_pnl": totals["total_net_pnl"],
                "total_trades": totals["total_trades"] or 0,
            },
        })


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
