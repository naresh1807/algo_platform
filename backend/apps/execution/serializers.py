from rest_framework import serializers

from .models import OpenPosition


class OpenPositionSerializer(serializers.ModelSerializer):
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = OpenPosition
        fields = "__all__"
