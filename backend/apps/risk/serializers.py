from rest_framework import serializers

from .models import AccountEquity, KillSwitchState, RiskEvent


class RiskEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = RiskEvent
        fields = "__all__"
        read_only_fields = ["created_at"]


class KillSwitchStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = KillSwitchState
        fields = "__all__"


class AccountEquitySerializer(serializers.ModelSerializer):
    class Meta:
        model = AccountEquity
        fields = "__all__"
