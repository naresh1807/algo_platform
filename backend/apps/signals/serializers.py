from rest_framework import serializers

from .models import TradingSignal


class TradingSignalSerializer(serializers.ModelSerializer):
    # Static option-contract identity fields only (underlying/expiry/
    # strike/option_type/symbol_token/tradingsymbol/lot_size) -- live
    # leg data (LTP/bid/ask/OI/volume/IV/greeks) is deliberately NOT
    # embedded here, since it would go stale immediately and duplicate
    # apps.options.views.OptionChainView's job; the frontend joins on
    # strike+option_type against that already-open chain view instead
    # (same pattern OptionsAnalytics.jsx already uses for its live
    # merge). Lazy import (function-local, inside the class body is
    # evaluated at class-definition time, so import here instead) to
    # avoid apps.signals depending on apps.options at Django
    # app-registry load time, same reasoning as this model's own
    # OptionSide TextChoices duplication (see models.py).
    option_contract_detail = serializers.SerializerMethodField()

    class Meta:
        model = TradingSignal
        fields = "__all__"
        read_only_fields = ["created_at"]

    def get_option_contract_detail(self, obj):
        if obj.option_contract_id is None:
            return None
        from apps.options.serializers import OptionContractSerializer

        return OptionContractSerializer(obj.option_contract).data
