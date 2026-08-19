from rest_framework import serializers

from .models import FeedHealthCheck, PriceAlert


class FeedHealthCheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = FeedHealthCheck
        fields = "__all__"


class PriceAlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = PriceAlert
        fields = "__all__"
        read_only_fields = ("created_by", "active", "triggered_at", "triggered_price")

    def validate_target_price(self, value):
        if value <= 0:
            raise serializers.ValidationError("Target price must be greater than zero.")
        return value
