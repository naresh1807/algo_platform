from rest_framework import serializers

from .models import DailyReviewNote, DriftEvent, HypotheticalTrade, ModelRegistry, StrategyVersion, TradeReview


class StrategyVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = StrategyVersion
        fields = "__all__"


class ModelRegistrySerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelRegistry
        # artifact_path is an internal server filesystem/object-store
        # location.  The dashboard needs model identity and metrics, not
        # deployment topology details.
        fields = (
            "id",
            "model_name",
            "model_version",
            "metrics_json",
            "active_flag",
            "created_at",
        )


class DailyReviewNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyReviewNote
        fields = "__all__"
        read_only_fields = ("review_date", "summary", "suggested_changes")


class DriftEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriftEvent
        fields = "__all__"


class TradeReviewSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="position.symbol", read_only=True)

    class Meta:
        model = TradeReview
        fields = "__all__"


class HypotheticalTradeSerializer(serializers.ModelSerializer):
    class Meta:
        model = HypotheticalTrade
        fields = "__all__"
