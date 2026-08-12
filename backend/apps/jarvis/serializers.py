from rest_framework import serializers

from .models import JarvisCommandHistory, JarvisMemory, MarketOutlookSnapshot


class JarvisCommandHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = JarvisCommandHistory
        fields = [
            "id", "raw_text", "input_mode", "intent_category", "intent_action",
            "executed_module", "result_text", "success", "needs_confirmation",
            "execution_ms", "created_at",
        ]


class JarvisMemorySerializer(serializers.ModelSerializer):
    class Meta:
        model = JarvisMemory
        fields = ["context_json", "updated_at"]


class JarvisCommandRequestSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=500)
    input_mode = serializers.ChoiceField(choices=["voice", "text", "chat"], default="text")
    confirm = serializers.BooleanField(default=False)


class MarketOutlookSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketOutlookSnapshot
        fields = ["id", "generated_at", "bias", "bias_confidence", "summary", "best_options_idea", "best_stock_idea"]
