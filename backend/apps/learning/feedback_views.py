from django.conf import settings
from django.db.models import Sum
from rest_framework.views import APIView
from rest_framework.response import Response
from common.permissions import IsTraderOrAdmin
from .models import RealizedTradeFeedback, TradeFeedbackRun


class TradeFeedbackView(APIView):
    permission_classes=[IsTraderOrAdmin]
    def get(self,request):
        samples=RealizedTradeFeedback.objects.filter(environment="paper")
        last=TradeFeedbackRun.objects.filter(kind="train").order_by("-created_at").first()
        return Response({"model_display_name":settings.TRADING_MODEL_DISPLAY_NAME,"scope":"Executed paper option trades only","samples":samples.count(),
             "wins":samples.filter(profitable=True).count(),"losses_or_flat":samples.filter(profitable=False).count(),
             "net_pnl":samples.aggregate(value=Sum("net_pnl"))["value"] or 0,
             "costs":samples.aggregate(value=Sum("costs"))["value"] or 0,
             "schedule":"Collect every 5 minutes; candidate training 18:15 IST weekdays",
             "last_training":{"at":last.created_at,"report":last.report} if last else None})
