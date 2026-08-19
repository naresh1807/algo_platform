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
        read_only_fields = ["changed_at", "changed_by"]

    def validate(self, attrs):
        instance = self.instance or OptionsStrategySetting()
        expiry_mode = attrs.get("expiry_mode", instance.expiry_mode)
        custom_expiry = attrs.get("custom_expiry", instance.custom_expiry)
        strike_mode = attrs.get("strike_mode", instance.strike_mode)
        steps = attrs.get("itm_otm_steps", instance.itm_otm_steps)

        if expiry_mode == OptionsStrategySetting.ExpiryMode.CUSTOM:
            if custom_expiry is None:
                raise serializers.ValidationError({"custom_expiry": "A custom expiry is required."})
            if not OptionContract.objects.filter(expiry=custom_expiry, is_active=True).exists():
                raise serializers.ValidationError(
                    {"custom_expiry": "Choose an active expiry that has already been synchronized."},
                )
        if strike_mode in (
            OptionsStrategySetting.StrikeMode.ITM,
            OptionsStrategySetting.StrikeMode.OTM,
        ) and not 1 <= steps <= 10:
            raise serializers.ValidationError({"itm_otm_steps": "Choose between 1 and 10 strike steps."})
        return attrs


class OptionChainSnapshotSerializer(serializers.ModelSerializer):
    strike = serializers.DecimalField(source="contract.strike", max_digits=12, decimal_places=2, read_only=True)
    option_type = serializers.CharField(source="contract.option_type", read_only=True)

    class Meta:
        model = OptionChainSnapshot
        fields = "__all__"
