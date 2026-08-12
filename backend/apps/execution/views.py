from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import OpenPosition
from .serializers import OpenPositionSerializer


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
