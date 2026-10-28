from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import WarHeadlinesView, MarketSummaryView, NewsImpactAnalysisViewSet, NewsSentimentViewSet

app_name = "news"

router = DefaultRouter()
router.register("sentiment", NewsSentimentViewSet, basename="news-sentiment")
router.register("impact", NewsImpactAnalysisViewSet, basename="news-impact")

urlpatterns = router.urls + [
    path("war-headlines/", WarHeadlinesView.as_view(), name="war-headlines"),
    path("market-summary/", MarketSummaryView.as_view(), name="market-summary"),
]
