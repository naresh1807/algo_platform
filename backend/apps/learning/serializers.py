from rest_framework import serializers

from .models import DailyReviewNote, DriftEvent, ModelRegistry, StrategyVersion, TradeReview


class StrategyVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = StrategyVersion
        fields = "__all__"


class ModelRegistrySerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelRegistry
        fields = "__all__"


class DailyReviewNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyReviewNote
        fields = "__all__"


class DriftEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriftEvent
        fields = "__all__"


class TradeReviewSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="position.symbol", read_only=True)

    class Meta:
        model = TradeReview
        fields = "__all__"
