from rest_framework import serializers

from .models import TradingSignal


class TradingSignalSerializer(serializers.ModelSerializer):
    class Meta:
        model = TradingSignal
        fields = "__all__"
        read_only_fields = ["created_at"]
