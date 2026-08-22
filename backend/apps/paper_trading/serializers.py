from rest_framework import serializers

from .models import (
    PaperAccount, PaperAIDecision, PaperDailySummary, PaperOrder,
    PaperPosition, PaperTrade, PaperTradeResult,
)


class PaperAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperAccount
        fields = "__all__"


class PaperOrderSerializer(serializers.ModelSerializer):
    contract_label = serializers.SerializerMethodField()

    class Meta:
        model = PaperOrder
        fields = "__all__"

    def get_contract_label(self, obj):
        return str(obj.contract)


class PaperPositionSerializer(serializers.ModelSerializer):
    contract_label = serializers.SerializerMethodField()

    class Meta:
        model = PaperPosition
        fields = "__all__"

    def get_contract_label(self, obj):
        return str(obj.contract)


class PaperTradeResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperTradeResult
        exclude = ["trade"]


class PaperTradeSerializer(serializers.ModelSerializer):
    result = PaperTradeResultSerializer(read_only=True)

    class Meta:
        model = PaperTrade
        fields = "__all__"


class PaperDailySummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperDailySummary
        fields = "__all__"


class PaperAIDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperAIDecision
        fields = "__all__"
