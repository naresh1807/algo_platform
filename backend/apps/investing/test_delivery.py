import uuid
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from apps.risk.models import KillSwitchState
from .models import Stock, StockPriceHistory, DeliveryPaperAccount, DeliveryHolding, DeliveryPaperOrder


class DeliveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("delivery-user", is_superuser=True)
        self.other = get_user_model().objects.create_user("delivery-other", is_superuser=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.stock = Stock.objects.create(symbol="TEST", name="Test equity")
        self.price = StockPriceHistory.objects.create(stock=self.stock, date=timezone.localdate(), open=100, high=100, low=100, close=100)

    def order(self, action="buy", quantity=10, request_id=None):
        return self.client.post("/api/investing/delivery/orders/", {"request_id": str(request_id or uuid.uuid4()), "stock_id": self.stock.id, "action": action, "quantity": quantity}, format="json")

    def test_buy_retry_and_sell(self):
        request_id = uuid.uuid4()
        self.assertEqual(self.order(request_id=request_id).status_code, 201)
        self.assertEqual(self.order(request_id=request_id).status_code, 200)
        self.assertEqual(DeliveryPaperOrder.objects.count(), 1)
        self.assertEqual(DeliveryPaperAccount.objects.get(user=self.user).cash, 99000)
        self.assertEqual(self.order("sell", 4).status_code, 201)
        self.assertEqual(DeliveryHolding.objects.get().quantity, 6)
        self.assertEqual(DeliveryPaperAccount.objects.get(user=self.user).cash, 99400)

    def test_oversell_insufficient_cash_and_invalid_quantity(self):
        for action, quantity in [("sell", 1), ("buy", 1001), ("buy", 0)]:
            self.assertEqual(self.order(action, quantity).status_code, 400)
        self.assertFalse(DeliveryPaperOrder.objects.exists())

    def test_request_id_conflict(self):
        request_id = uuid.uuid4()
        self.order(request_id=request_id)
        self.assertEqual(self.order(quantity=11, request_id=request_id).status_code, 409)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.order(request_id=request_id).status_code, 409)

    def test_account_and_orders_are_private(self):
        self.order()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/investing/delivery/orders/").data["results"], [])
        account = self.client.get("/api/investing/delivery/account/").data
        self.assertEqual(account["holdings"], [])
        self.assertEqual(account["cash"], 100000)

    def test_stale_quote_and_kill_switch(self):
        self.price.date = timezone.localdate() - timedelta(days=8)
        self.price.save()
        self.assertEqual(self.order().status_code, 400)
        self.price.date = timezone.localdate()
        self.price.save()
        self.order()
        KillSwitchState.objects.create(is_active=True)
        self.assertEqual(self.order().status_code, 400)
        self.assertEqual(self.order("sell", 1).status_code, 201)

    def test_weighted_average_and_realized_profit(self):
        self.order(quantity=10)
        self.price.close = 120
        self.price.save()
        self.order(quantity=10)
        self.assertEqual(DeliveryHolding.objects.get().average_price, 110)
        self.order("sell", 5)
        holding = DeliveryHolding.objects.get()
        self.assertEqual(holding.quantity, 15)
        self.assertEqual(holding.realized_pnl, 50)

    def test_authentication_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/investing/delivery/account/").status_code, 401)
