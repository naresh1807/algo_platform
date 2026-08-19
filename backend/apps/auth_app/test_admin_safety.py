from types import SimpleNamespace

from django.contrib.admin.sites import AdminSite
from django.test import SimpleTestCase

from apps.execution.admin import OpenPositionAdmin
from apps.execution.models import OpenPosition
from apps.risk.admin import AccountEquityAdmin, KillSwitchStateAdmin, RiskEventAdmin
from apps.risk.models import AccountEquity, KillSwitchState, RiskEvent
from apps.signals.admin import TradingSignalAdmin
from apps.signals.models import TradingSignal


class OperationalAdminSafetyTests(SimpleTestCase):
    ADMIN_MODELS = (
        (KillSwitchStateAdmin, KillSwitchState),
        (AccountEquityAdmin, AccountEquity),
        (RiskEventAdmin, RiskEvent),
        (TradingSignalAdmin, TradingSignal),
        (OpenPositionAdmin, OpenPosition),
    )

    def test_operational_admins_are_inspection_only(self):
        request = SimpleNamespace(user=SimpleNamespace())
        for admin_class, model in self.ADMIN_MODELS:
            with self.subTest(model=model.__name__):
                model_admin = admin_class(model, AdminSite())
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))
                self.assertIsNone(model_admin.actions)

    def test_every_persisted_field_is_read_only(self):
        request = SimpleNamespace(user=SimpleNamespace())
        for admin_class, model in self.ADMIN_MODELS:
            with self.subTest(model=model.__name__):
                model_admin = admin_class(model, AdminSite())
                readonly = set(model_admin.get_readonly_fields(request))
                persisted = {field.name for field in model._meta.fields}
                self.assertTrue(persisted.issubset(readonly))
