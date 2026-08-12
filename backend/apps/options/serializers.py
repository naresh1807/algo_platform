from rest_framework import serializers

from .models import OptionChainSnapshot, OptionContract


class OptionContractSerializer(serializers.ModelSerializer):
    class Meta:
        model = OptionContract
        fields = "__all__"


class OptionChainSnapshotSerializer(serializers.ModelSerializer):
    strike = serializers.DecimalField(source="contract.strike", max_digits=12, decimal_places=2, read_only=True)
    option_type = serializers.CharField(source="contract.option_type", read_only=True)

    class Meta:
        model = OptionChainSnapshot
        fields = "__all__"
