from collections import Counter
from datetime import timedelta
from zoneinfo import ZoneInfo
from django.shortcuts import get_object_or_404
from django.conf import settings
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from common.permissions import IsTraderOrAdmin
from apps.signals.models import TradingSignal
from apps.learning.models import RealizedTradeFeedback
from .models import PaperAIDecision, PaperTrade
from .serializers import PaperAIDecisionSerializer


def rejection_category(reason):
    if "under one lot" in reason: return "Below one exchange lot"
    if "Broker feed reported unhealthy" in reason: return "Broker feed unavailable"
    if "isn't profitable enough" in reason: return "Historical profitability check"
    if "headline vetoes" in reason: return "Sentiment veto"
    if "no clear directional bias" in reason: return "Unclear direction"
    return "Other checks"


class DrishtiMonitoringView(APIView):
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        today = timezone.localdate(timezone=ZoneInfo("Asia/Kolkata"))
        signals = TradingSignal.objects.filter(created_at__date=today)
        decisions = PaperAIDecision.objects.filter(timestamp__date=today).select_related("contract")
        reasons = Counter(rejection_category(r) for r in signals.filter(status="rejected").values_list("reason", flat=True))
        trades = list(PaperTrade.objects.filter(closed_at__date=today).select_related("position__contract", "result").order_by("-closed_at")[:50])
        entry_ids = [t.position.entry_decision_id for t in trades if t.position.entry_decision_id]
        entries = {d.decision_id: d for d in PaperAIDecision.objects.filter(decision_id__in=entry_ids)}
        learned = set(RealizedTradeFeedback.objects.filter(source="paper_ai_trade", source_id__in=[t.pk for t in trades]).values_list("source_id", flat=True))
        journal = []
        for t in trades:
            pos = t.position
            entry = entries.get(pos.entry_decision_id)
            journal.append({"id":t.pk,"contract":str(pos.contract),"symbol":pos.contract.underlying,
                "opened_at":pos.opened_at,"closed_at":t.closed_at,"entry_price":t.entry_price,"exit_price":t.exit_price,
                "quantity":t.quantity,"stop":pos.initial_stop_loss,"gross_pnl":t.gross_pnl,"net_pnl":t.net_pnl,
                "costs":t.gross_pnl-t.net_pnl,"model_version":entry.model_version if entry else None,
                "entry_evidence":entry.explanation_json if entry else None,
                "learning_status":"Collected for learning" if t.pk in learned else "Awaiting collection or valid entry features",
                "exit_reason":t.result.exit_reason if hasattr(t,"result") else None})
        return Response({"name":settings.TRADING_MODEL_DISPLAY_NAME,"date":today,
            "execution_signal_count":signals.count(),"rejected_signal_count":signals.filter(status="rejected").count(),
            "blockers":dict(reasons),"decision_actions":dict(Counter(decisions.values_list("action",flat=True))),
            "decisions":PaperAIDecisionSerializer(decisions.order_by("-timestamp")[:30],many=True).data,
            "journal":journal,"scope":"Autonomous paper AI journal; execution signal blockers shown separately"})


class JournalChartView(APIView):
    permission_classes = [IsTraderOrAdmin]

    def get(self, request, trade_id):
        from apps.options.models import OptionCandle
        trade = get_object_or_404(PaperTrade.objects.select_related("position"), pk=trade_id)
        candles = OptionCandle.objects.filter(contract_id=trade.position.contract_id, timeframe="1m",
            timestamp__gte=trade.position.opened_at-timedelta(minutes=10),
            timestamp__lte=trade.closed_at+timedelta(minutes=10)).order_by("timestamp")
        return Response({"results":list(candles.values("timestamp","open","high","low","close","volume")[:500]),
                         "source":"Stored exact-contract one-minute candles"})
