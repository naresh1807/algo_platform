from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import engine
from .commands import CATEGORIES, suggested_commands
from .memory import get_memory
from .models import JarvisCommandHistory, MarketOutlookSnapshot
from .serializers import (
    JarvisCommandHistorySerializer,
    JarvisCommandRequestSerializer,
    JarvisMemorySerializer,
    MarketOutlookSnapshotSerializer,
)


class JarvisCommandView(APIView):
    """
    POST /api/jarvis/command/ -- the one endpoint that powers the whole
    JarvisPanel (manual 14.19): send whatever text was typed or
    transcribed from voice, get back a structured response (text +
    optional route to navigate to + confirmation flag).
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = JarvisCommandRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = engine.process(
            serializer.validated_data["text"],
            request.user,
            input_mode=serializer.validated_data["input_mode"],
            confirm=serializer.validated_data["confirm"],
        )
        return Response(result.to_dict(), status=status.HTTP_200_OK)


class JarvisHistoryListView(generics.ListAPIView):
    """
    GET /api/jarvis/history/ -- manual 14.19's Conversation History panel.
    DELETE /api/jarvis/history/ -- clears the requesting user's own
    JARVIS chat history (and only theirs -- scoped by request.user the
    same way get_queryset already is, never another user's rows).
    """

    serializer_class = JarvisCommandHistorySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return JarvisCommandHistory.objects.filter(user=self.request.user)[:100]

    def delete(self, request):
        # Not get_queryset() -- that's sliced to the most recent 100 for
        # the list view, but "clear my history" should delete every row,
        # not just the ones the panel currently displays.
        deleted, _ = JarvisCommandHistory.objects.filter(user=request.user).delete()
        return Response({"deleted": deleted}, status=status.HTTP_200_OK)


class JarvisMemoryView(APIView):
    """GET /api/jarvis/memory/ -- current context (manual 14.17)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        memory = get_memory(request.user)
        return Response(JarvisMemorySerializer(memory).data)


class JarvisSuggestedCommandsView(APIView):
    """GET /api/jarvis/suggested/ -- manual 14.19 Suggested Commands panel."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        category = request.query_params.get("category")
        if category and category not in CATEGORIES:
            return Response({"detail": f"Unknown category. Choose from {CATEGORIES}."}, status=400)
        return Response({"category": category, "commands": suggested_commands(category)})


class MarketOutlookView(APIView):
    """
    GET /api/jarvis/market-outlook/ -- the AI/ML synthesis report
    (apps.jarvis.market_intelligence), trader-requested and not
    manual-specified. Returns the latest stored snapshot
    (apps.jarvis.tasks.generate_market_outlook's periodic output);
    computes one on the spot if nothing has been generated yet, same
    fallback the market_outlook JARVIS command itself uses.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        latest = MarketOutlookSnapshot.objects.order_by("-generated_at").first()
        if latest is not None:
            return Response(MarketOutlookSnapshotSerializer(latest).data)

        from .market_intelligence import market_outlook
        result = market_outlook()
        return Response({
            "id": None, "generated_at": None, "bias": result["bias"],
            "bias_confidence": result["bias_confidence"], "summary": result["summary"],
            "best_options_idea": (result["options_idea"] or {}).get("strike", ""),
            "best_stock_idea": (result["stock_idea"] or {}).get("symbol", ""),
        })


class MarketOutlookHistoryView(generics.ListAPIView):
    """GET /api/jarvis/market-outlook/history/ -- how the bias has moved through the day."""

    queryset = MarketOutlookSnapshot.objects.all()[:50]
    serializer_class = MarketOutlookSnapshotSerializer
    permission_classes = [permissions.IsAuthenticated]
