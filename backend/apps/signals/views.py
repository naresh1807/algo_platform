from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import TradingSignal
from .serializers import TradingSignalSerializer


class TradingSignalViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only from the API: signals are only ever created by the signal
    engine (apps/signals/engine.py, not yet written), which runs the
    technical + sentiment + regime logic from manual sections 11-12 and
    then asks apps.risk to approve/reject before writing a row here.
    """
    queryset = TradingSignal.objects.select_related("strategy_version", "option_contract").all()
    serializer_class = TradingSignalSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["symbol", "status", "signal_type"]
