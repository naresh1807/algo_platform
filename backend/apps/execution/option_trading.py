"""Manual call/put purchases and full-position sells through existing executors."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.market_data.market_hours import is_market_open
from apps.options.expiry_service import is_expiry_eligible
from apps.options.models import OptionContract
from apps.risk.engine import check_option_contract_liquidity, check_pre_trade
from apps.signals.models import TradingSignal
from common.constants import MarketRegime, PositionSide, SignalStatus, SignalType

from .models import BrokerOrder, ExecutionModeSetting, OpenPosition


class OptionTicketSerializer(serializers.Serializer):
    contract_id = serializers.IntegerField(min_value=1)
    lots = serializers.IntegerField(min_value=1, max_value=10000)
    stop_loss = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    target_price = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    execution_mode = serializers.ChoiceField(choices=["paper", "live"])


class OptionOrderSerializer(OptionTicketSerializer):
    request_id = serializers.UUIDField()
    action = serializers.ChoiceField(choices=["buy", "sell"])
    contract_id = serializers.IntegerField(min_value=1, required=False)
    lots = serializers.IntegerField(min_value=1, max_value=10000, required=False)
    stop_loss = serializers.DecimalField(max_digits=14, decimal_places=4, required=False, min_value=Decimal("0.0001"))
    target_price = serializers.DecimalField(max_digits=14, decimal_places=4, required=False, min_value=Decimal("0.0001"))
    position_id = serializers.IntegerField(min_value=1, required=False)
    confirm_live = serializers.BooleanField(default=False)

    def validate(self, data):
        fields = {"contract_id", "lots", "stop_loss", "target_price"}
        if data["action"] == "buy":
            missing = fields - data.keys()
            if missing:
                raise serializers.ValidationError(f"Required: {', '.join(sorted(missing))}.")
            if "position_id" in data:
                raise serializers.ValidationError("Buy orders cannot include position_id.")
        elif "position_id" not in data or fields.intersection(data):
            raise serializers.ValidationError("Sell requires position_id and closes its full quantity.")
        if data["execution_mode"] == "live" and not data["confirm_live"]:
            raise serializers.ValidationError("Confirm the live order before submission.")
        return data


def current_quote(contract):
    snapshot = contract.snapshots.order_by("-timestamp").first()
    now = timezone.now()
    if snapshot is None or now - snapshot.timestamp > timedelta(seconds=60) or snapshot.timestamp > now + timedelta(seconds=5):
        raise ValueError("A quote from the last 60 seconds is required. Wait for the option feed.")
    if not snapshot.ltp.is_finite() or snapshot.ltp <= 0:
        raise ValueError("The option premium must be positive.")
    return snapshot


def validate_ticket(data):
    contract = OptionContract.objects.get(pk=data["contract_id"])
    if not contract.is_active or not is_expiry_eligible(contract.expiry):
        raise ValueError("This option contract is inactive or expired.")
    if not contract.tradingsymbol or not contract.symbol_token or contract.lot_size <= 0:
        raise ValueError("The contract needs a trading symbol, broker token, and lot size.")
    market_open, reason = is_market_open()
    if not market_open:
        raise ValueError(f"Market is closed: {reason}")
    snapshot = current_quote(contract)
    if snapshot.bid is None or snapshot.ask is None or snapshot.bid <= 0 or snapshot.ask < snapshot.bid:
        raise ValueError("A positive, uncrossed bid/ask quote is required.")
    ok, reason = check_option_contract_liquidity(
        contract, float(snapshot.bid), float(snapshot.ask), snapshot.open_interest, snapshot.volume,
    )
    if not ok:
        raise ValueError(reason)
    premium = max(snapshot.ltp, snapshot.ask)
    if not data["stop_loss"] < premium < data["target_price"]:
        raise ValueError("Stop-loss must be below the option premium and target must be above it.")
    decision = check_pre_trade(contract.underlying, premium, data["stop_loss"])
    qty = data["lots"] * contract.lot_size
    if not decision.approved:
        raise ValueError(decision.reason_text)
    if qty > decision.position_size:
        raise ValueError(f"Requested quantity {qty} exceeds the risk allowance ({int(decision.position_size) // contract.lot_size} lots).")
    return contract, snapshot, premium, qty


def preview_ticket(data):
    from .models import get_execution_mode

    if data["execution_mode"] != get_execution_mode():
        raise ValueError("Execution mode changed. Refresh and review the order again.")
    if data["execution_mode"] == "live" and not settings.LIVE_TRADING_ENABLED:
        raise ValueError("Live trading is disarmed by server configuration.")
    contract, snapshot, premium, qty = validate_ticket(data)
    return {
        "contract_id": contract.pk, "tradingsymbol": contract.tradingsymbol,
        "option_type": contract.option_type, "lot_size": contract.lot_size,
        "quantity": qty, "premium": str(premium), "quote_at": snapshot.timestamp.isoformat(),
        "premium_outlay": str(premium * qty),
        "stop_risk": str((premium - data["stop_loss"]) * qty),
        "target_profit": str((data["target_price"] - premium) * qty),
        "execution_mode": data["execution_mode"],
    }


def execute_buy(order, data):
    from django.core.exceptions import ObjectDoesNotExist
    from .models import get_execution_mode
    from .paper_executor import open_position_from_signal

    if get_execution_mode() != data["execution_mode"]:
        raise ValueError("Execution mode changed. Refresh and review the order again.")
    if data["execution_mode"] == "live":
        if not settings.LIVE_TRADING_ENABLED:
            raise ValueError("Live trading is disarmed by server configuration.")
        from .live_executor import reconcile_broker_orders, reconcile_broker_positions, sync_live_account_equity
        reconcile_broker_orders()
        if BrokerOrder.objects.filter(status__in=["created", "submitted", "unknown"]).exists():
            raise ValueError("Resolve outstanding broker orders before opening a new position.")
        ok, reason = reconcile_broker_positions()
        if not ok:
            raise ValueError(reason)
        if not OpenPosition.objects.filter(execution_mode="live", closed_at__isnull=True).exists():
            sync = sync_live_account_equity()
            if not sync.get("synced"):
                raise ValueError(f"Broker equity synchronization failed: {sync.get('reason', '')}")

    # Preserve risk events and kill-switch changes even if a ticket is rejected.
    # Paper approval and execution commit together under the shared mode lock.
    rejection = None
    position = None
    with transaction.atomic():
        mode, _ = ExecutionModeSetting.objects.select_for_update().get_or_create(pk=1)
        try:
            if mode.mode != data["execution_mode"]:
                raise ValueError("Execution mode changed. Refresh and review the order again.")
            contract, snapshot, premium, qty = validate_ticket(data)
        except (ValueError, ObjectDoesNotExist) as exc:
            rejection = exc
        else:
            signal = TradingSignal.objects.create(
                symbol=contract.underlying, signal_type=SignalType.BUY,
                option_contract=contract, option_side=contract.option_type, strike_price=contract.strike,
                entry_price=premium, stop_loss=data["stop_loss"], target_1=data["target_price"],
                position_size=qty, total_score=0, technical_score=0, sentiment_score=0,
                risk_score=1, options_score=0, regime=MarketRegime.SIDEWAYS,
                execution_mode=mode.mode, status=SignalStatus.APPROVED,
                reason=f"Manual option buy by {order.actor.get_username()}; request {order.pk}.",
            )
            order.signal = signal
            order.save(update_fields=["signal"])
            if mode.mode == "paper":
                try:
                    position = open_position_from_signal(signal, enforce_execution_risk=True)
                except ValueError as exc:
                    rejection = exc
    if rejection is not None:
        raise rejection
    if position is not None:
        return position
    from .live_executor import open_position_live
    return open_position_live(signal)

def execute_sell(order, data):
    position = OpenPosition.objects.select_related("option_contract").get(pk=data["position_id"])
    if position.option_contract_id is None or position.side != PositionSide.LONG:
        raise ValueError("Sell to close requires an existing long call or put position.")
    if position.execution_mode != data["execution_mode"]:
        raise ValueError("The ticket mode does not match this position's execution mode.")
    order.position = position
    order.save(update_fields=["position"])
    if position.closed_at is not None:
        return position
    market_open, reason = is_market_open()
    if not market_open:
        raise ValueError(f"Market is closed: {reason}")
    reason = f"Manual sell to close by {order.actor.get_username()}; request {order.pk}"
    # Reducing exposure remains possible while the kill switch is active.
    if position.execution_mode == "paper":
        from .paper_executor import close_position
        snapshot = current_quote(position.option_contract)
        price = snapshot.bid if snapshot.bid is not None and snapshot.bid > 0 else snapshot.ltp
        close_position(position, price, reason)
    else:
        from .live_executor import close_position_live
        close_position_live(position, reason)
    position.refresh_from_db()
    return position

