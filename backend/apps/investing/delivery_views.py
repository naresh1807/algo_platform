"""Independent user-owned paper delivery ledger, using dated daily-close prices.
No broker orders, shared options equity, or live-market fills are represented here.
"""
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from common.permissions import IsTraderOrAdmin
from apps.risk.models import KillSwitchState
from .models import Stock, StockWatchlist, DeliveryPaperAccount, DeliveryHolding, DeliveryPaperOrder


def stock_data(stock):
    price = stock.price_history.filter(date__lte=timezone.localdate()).order_by("-date").first()
    return {"id": stock.id, "symbol": stock.symbol, "name": stock.name, "exchange": stock.exchange,
            "price": price.close if price else None, "price_date": price.date if price else None}


def order_data(order):
    return {"request_id": str(order.request_id), "symbol": order.stock.symbol,
            "action": order.action, "quantity": order.quantity, "price": order.price,
            "reference_date": order.reference_date, "created_at": order.created_at,
            "status": "executed", "mode": "paper"}


class DeliveryTicket(serializers.Serializer):
    request_id = serializers.UUIDField()
    stock_id = serializers.IntegerField(min_value=1)
    action = serializers.ChoiceField(choices=["buy", "sell"])
    quantity = serializers.IntegerField(min_value=1, max_value=100000)


class DeliveryStocksView(APIView):
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        query = request.query_params.get("search", "").strip()[:64]
        stocks = Stock.objects.filter(Q(symbol__icontains=query) | Q(name__icontains=query))
        if request.query_params.get("watchlist") == "true":
            stocks = stocks.filter(id__in=StockWatchlist.objects.filter(user=request.user).values("stock_id"))
        stocks = stocks[:30]
        return Response({"results": [stock_data(stock) for stock in stocks]})


class DeliveryAccountView(APIView):
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        account, _ = DeliveryPaperAccount.objects.get_or_create(user=request.user)
        holdings = []
        value = Decimal("0")
        for holding in DeliveryHolding.objects.filter(account=account, quantity__gt=0).select_related("stock"):
            stock = stock_data(holding.stock)
            price = stock["price"]
            mark = price if price is not None else holding.average_price
            value += mark * holding.quantity
            holdings.append({**stock, "quantity": holding.quantity, "average_price": holding.average_price,
                             "unrealized_pnl": (mark - holding.average_price) * holding.quantity})
        realized = sum(DeliveryHolding.objects.filter(account=account).values_list("realized_pnl", flat=True), Decimal("0"))
        return Response({"mode": "paper", "cash": account.cash, "holdings_value": value,
                         "portfolio_value": account.cash + value, "realized_pnl": realized, "holdings": holdings,
                         "pricing": "Latest stored daily close; not a live market fill. Fees excluded."})


class DeliveryOrdersView(APIView):
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        orders = DeliveryPaperOrder.objects.filter(account__user=request.user).select_related("stock")[:100]
        return Response({"results": [order_data(order) for order in orders]})

    def post(self, request):
        serializer = DeliveryTicket(data=request.data)
        serializer.is_valid(raise_exception=True)
        ticket = serializer.validated_data
        with transaction.atomic():
            account, _ = DeliveryPaperAccount.objects.get_or_create(user=request.user)
            account = DeliveryPaperAccount.objects.select_for_update().get(pk=account.pk)
            existing = DeliveryPaperOrder.objects.filter(request_id=ticket["request_id"]).first()
            if existing:
                if existing.account_id != account.id or (existing.stock_id, existing.action, existing.quantity) != (ticket["stock_id"], ticket["action"], ticket["quantity"]):
                    return Response({"error": "Request ID already used for a different order."}, status=409)
                return Response(order_data(existing))
            stock = Stock.objects.filter(pk=ticket["stock_id"]).first()
            if not stock:
                return Response({"error": "Stock not found."}, status=404)
            quote = stock.price_history.filter(date__lte=timezone.localdate()).order_by("-date").first()
            if not quote or quote.close <= 0 or quote.date < timezone.localdate() - timedelta(days=7):
                return Response({"error": "A stored daily close from the last seven days is required."}, status=400)
            quantity = ticket["quantity"]
            amount = (quote.close * quantity).quantize(Decimal("0.01"))
            holding, _ = DeliveryHolding.objects.get_or_create(account=account, stock=stock)
            if ticket["action"] == "buy":
                if KillSwitchState.objects.filter(is_active=True).exists():
                    return Response({"error": "Kill switch active. New buys are blocked."}, status=400)
                if amount > account.cash:
                    return Response({"error": "Insufficient delivery paper cash."}, status=400)
                holding.average_price = ((holding.average_price * holding.quantity + amount) / (holding.quantity + quantity)).quantize(Decimal("0.01"))
                holding.quantity += quantity
                account.cash -= amount
            else:
                if quantity > holding.quantity:
                    return Response({"error": "Cannot sell more shares than you hold."}, status=400)
                holding.quantity -= quantity
                holding.realized_pnl += (quote.close - holding.average_price) * quantity
                account.cash += amount
                if holding.quantity == 0:
                    holding.average_price = 0
            holding.save()
            account.save(update_fields=["cash"])
            order = DeliveryPaperOrder.objects.create(account=account, request_id=ticket["request_id"], stock=stock,
                       action=ticket["action"], quantity=quantity, price=quote.close, reference_date=quote.date)
        return Response(order_data(order), status=201)
