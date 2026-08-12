from rest_framework import serializers

from .models import HistoricalData


class HistoricalDataSerializer(serializers.ModelSerializer):
    class Meta:
        model = HistoricalData
        fields = "__all__"
