from rest_framework import serializers

from .models import ExecutionModeSetting, OpenPosition


class OpenPositionSerializer(serializers.ModelSerializer):
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = OpenPosition
        fields = "__all__"


class ExecutionModeSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionModeSetting
        fields = "__all__"
