from rest_framework import serializers

from .models import OptionChainSnapshot, OptionContract, OptionsStrategySetting


class OptionContractSerializer(serializers.ModelSerializer):
    class Meta:
        model = OptionContract
        fields = "__all__"


class OptionsStrategySettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = OptionsStrategySetting
        fields = "__all__"
        read_only_fields = ["changed_at"]


class OptionChainSnapshotSerializer(serializers.ModelSerializer):
    strike = serializers.DecimalField(source="contract.strike", max_digits=12, decimal_places=2, read_only=True)
    option_type = serializers.CharField(source="contract.option_type", read_only=True)

    class Meta:
        model = OptionChainSnapshot
        fields = "__all__"
