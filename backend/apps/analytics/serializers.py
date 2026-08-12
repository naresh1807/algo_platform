from rest_framework import serializers

from .models import PerformanceMetrics


class PerformanceMetricsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PerformanceMetrics
        fields = "__all__"
