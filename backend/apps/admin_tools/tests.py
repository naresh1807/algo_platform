from django.contrib.auth import get_user_model
from django.test import TestCase

from .audit import log_action
from .models import AuditLog


class LogActionTests(TestCase):
    def test_creates_an_audit_log_entry(self):
        log_action(action="test_action", actor_label="unit_test")
        self.assertEqual(AuditLog.objects.filter(action="test_action").count(), 1)

    def test_records_actor_user_when_given(self):
        User = get_user_model()
        user = User.objects.create_user(username="tester", password="x")
        log_action(action="test_action", actor=user)
        entry = AuditLog.objects.get(action="test_action")
        self.assertEqual(entry.actor, user)

    def test_records_target_type_and_id(self):
        User = get_user_model()
        user = User.objects.create_user(username="tester2", password="x")
        log_action(action="test_action", target=user)
        entry = AuditLog.objects.get(action="test_action", target_id=str(user.pk))
        self.assertEqual(entry.target_type, "User")

    def test_never_raises_even_with_bad_input(self):
        """
        The whole design point of log_action() (see its docstring) is
        that a failure to write an audit entry must never propagate up
        and block the real action being logged -- this exercises that
        by passing something that can't be logged sensibly and
        confirming no exception escapes.
        """
        try:
            log_action(action="test_action", details={"unserializable": object()})
        except Exception as exc:
            self.fail(f"log_action() raised unexpectedly: {exc}")
