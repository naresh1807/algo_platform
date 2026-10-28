from rest_framework.views import APIView
from rest_framework.response import Response
from common.permissions import IsTraderOrAdmin
from .models import BTSTRun, BTSTSignal
from .btst import STRATEGIES


class BTSTView(APIView):
    permission_classes=[IsTraderOrAdmin]
    def get(self,request):
        signals=BTSTSignal.objects.filter(stock__watchers__user=request.user).distinct().select_related("stock","run").order_by("-created_at")[:100]
        runs=BTSTRun.objects.order_by("-trading_date")[:10]
        return Response({"schedule":"14:30 Asia/Kolkata on NSE equity market days", "execution":"signals only", "strategies":STRATEGIES,
                         "results":[{"id":s.pk,"symbol":s.stock.symbol,"date":s.run.trading_date,"entry_reference":s.entry_reference,
                         "stop_loss":s.stop_loss,"target_price":s.target_price,"exit_session":s.exit_session,"evidence":s.evidence} for s in signals],
                         "runs":[{"date":r.trading_date,"status":r.status,"scanned":r.scanned,"signals":r.signals.count()} for r in runs]})
