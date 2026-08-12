from rest_framework import serializers

from .models import NewsImpactAnalysis, NewsSentiment


class NewsImpactAnalysisSerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsImpactAnalysis
        fields = "__all__"


class NewsSentimentSerializer(serializers.ModelSerializer):
    # Nested, read-only -- lets the frontend get sentiment + impact in
    # one call for the "Output format" the manual section 10 describes
    # (sectors, likely rising/falling stocks, index impact, time
    # horizon, trade relevance all alongside the sentiment score),
    # without a second round-trip per headline.
    impact_analysis = NewsImpactAnalysisSerializer(read_only=True)

    class Meta:
        model = NewsSentiment
        fields = "__all__"
