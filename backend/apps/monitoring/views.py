from rest_framework import viewsets

from common.permissions import IsTraderOrAdmin

from .models import FeedHealthCheck, PriceAlert
from .serializers import FeedHealthCheckSerializer, PriceAlertSerializer


class FeedHealthCheckViewSet(viewsets.ReadOnlyModelViewSet):
    # RBAC: was plain IsAuthenticated -- aligned with the rest of the
    # dashboard's Trader/Admin-only read surface (common/permissions.py).
    queryset = FeedHealthCheck.objects.all()
    serializer_class = FeedHealthCheckSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["source", "is_healthy"]


class PriceAlertViewSet(viewsets.ModelViewSet):
    """
    Full CRUD (unlike FeedHealthCheck's read-only) -- creating/deleting
    an alert is exactly the trader action this feature exists for.
    Scoped to the requesting user's own alerts, same "each trader's own
    workspace" boundary as everything else that isn't shared platform
    state (risk limits, kill switch) -- one person's price watch on
    NIFTY shouldn't clutter another's alert list. RBAC: Trader or Admin,
    same as FeedHealthCheckViewSet above (was plain IsAuthenticated).
    """
    serializer_class = PriceAlertSerializer
    permission_classes = [IsTraderOrAdmin]

    def get_queryset(self):
        return PriceAlert.objects.filter(created_by=self.request.user)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
