"""Option order entry, previews, and attributed request history."""
import logging
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_tools.audit import log_action
from common.constants import SignalStatus
from common.permissions import IsTraderOrAdmin

from .models import BrokerOrder, OptionTradeRequest
from .option_trading import OptionOrderSerializer, OptionTicketSerializer, execute_buy, execute_sell, preview_ticket
from .serializers import OpenPositionSerializer

logger = logging.getLogger(__name__)


def order_data(order):
    # Recover the visible result after a request disconnect or process crash.
    # Broker submission is never repeated by a GET or duplicate POST.
    if order.signal_id and order.payload["action"] == "buy":
        position = order.signal.positions if hasattr(order.signal, "positions") else None
        if position is not None:
            order.position = position
            order.status = "executed"
            order.message = "Option bought."
        elif order.signal.status in [SignalStatus.REJECTED, SignalStatus.EXPIRED]:
            order.status = "rejected"
            order.message = order.signal.reason
    elif order.position_id and order.position.closed_at is not None:
        order.status = "executed"
        order.message = "Position sold to close."
    return {
        "request_id": str(order.pk), "action": order.payload["action"],
        "status": order.status, "execution_mode": order.execution_mode,
        "message": order.message, "created_at": order.created_at,
        "position": OpenPositionSerializer(order.position).data if order.position_id else None,
        "signal_id": order.signal_id,
    }


class OptionPreviewView(APIView):
    permission_classes = [IsTraderOrAdmin]

    def post(self, request):
        serializer = OptionTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return Response(preview_ticket(serializer.validated_data))
        except (ValueError, ObjectDoesNotExist) as exc:
            return Response({"error": str(exc)}, status=400)


class OptionOrdersView(APIView):
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        rows = OptionTradeRequest.objects.filter(actor=request.user).select_related(
            "signal", "signal__positions", "position",
        )[:100]
        return Response({"results": [order_data(row) for row in rows]})

    def post(self, request):
        serializer = OptionOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        payload = {key: str(value) if isinstance(value, Decimal) else value for key, value in data.items() if key != "request_id"}
        with transaction.atomic():
            order, created = OptionTradeRequest.objects.get_or_create(
                pk=data["request_id"],
                defaults={"actor": request.user, "payload": payload, "execution_mode": data["execution_mode"]},
            )
        if not created:
            if order.actor_id != request.user.pk or order.payload != payload:
                return Response({"error": "This request ID already belongs to a different order."}, status=409)
            return Response(order_data(order))

        log_action("manual_option_requested", actor=request.user, target=order, details=payload)
        try:
            position = execute_buy(order, data) if data["action"] == "buy" else execute_sell(order, data)
            order.position = position
            order.status = "executed"
            order.message = "Option bought." if data["action"] == "buy" else "Position sold to close."
        except Exception as exc:
            if isinstance(exc, (ValueError, PermissionError, ObjectDoesNotExist)):
                logger.warning("Option request %s rejected: %s", order.pk, exc)
            else:
                logger.exception("Option request %s failed", order.pk)
            # An uncertain broker order must remain uncertain, never invite a
            # second submission under a new UUID.
            order.refresh_from_db()
            pending = BrokerOrder.objects.filter(status__in=["created", "submitted", "unknown"])
            pending = pending.filter(signal_id=order.signal_id) if order.signal_id else pending.filter(position_id=order.position_id) if order.position_id else pending.none()
            uncertain = pending.exists() or not isinstance(exc, (ValueError, PermissionError, ObjectDoesNotExist))
            order.status = "unknown" if uncertain else "rejected"
            order.message = "Order state needs reconciliation. Check order history before taking further action." if uncertain else str(exc)
            if order.signal_id and not uncertain:
                # A failed request must not leave an approval for a worker.
                type(order.signal).objects.filter(pk=order.signal_id, status__in=["pending", "approved"]).update(
                    status=SignalStatus.REJECTED, reason=order.message,
                )
        order.save(update_fields=["position", "status", "message"])
        log_action("manual_option_result", actor=request.user, target=order, details={"status": order.status, "message": order.message})
        body = order_data(order)
        return Response(body, status=201 if body["status"] == "executed" else 202 if body["status"] in ["processing", "unknown"] else 400)

