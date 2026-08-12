from django.contrib import admin

from .models import NewsImpactAnalysis, NewsSentiment


@admin.register(NewsSentiment)
class NewsSentimentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "sentiment_label", "sentiment_score", "confidence", "published_at", "source")
    list_filter = ("sentiment_label", "news_category")
    search_fields = ("headline", "symbol")


@admin.register(NewsImpactAnalysis)
class NewsImpactAnalysisAdmin(admin.ModelAdmin):
    list_display = ("news", "trade_relevance", "time_horizon", "index_impact", "confidence")
    list_filter = ("trade_relevance", "time_horizon")
