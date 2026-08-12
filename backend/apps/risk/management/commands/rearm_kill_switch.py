"""
Manual-only kill-switch re-arming (manual section 21: "Never
auto-change core safety rules" -- re-arming after a trip is exactly the
kind of core safety state change that must never happen automatically).

Deliberately a management command, not a DRF endpoint: running this
requires shell access to the machine the platform is deployed on, which
is a meaningfully higher bar than clicking a button in a web UI. There
is intentionally no "reactivate" REST endpoint anywhere in apps/risk/urls.py.

Usage:
    python manage.py rearm_kill_switch --by "your name" [--confirm]
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.admin_tools.audit import log_action
from apps.risk.models import KillSwitchState


class Command(BaseCommand):
    help = "Manually deactivate an active kill switch. Requires --confirm."

    def add_arguments(self, parser):
        parser.add_argument(
            "--by", required=True,
            help="Who is re-arming this (recorded on KillSwitchState.deactivated_by for the audit trail).",
        )
        parser.add_argument(
            "--confirm", action="store_true",
            help="Required flag -- running without it only shows what would happen, without doing it.",
        )

    def handle(self, *args, **options):
        state, _ = KillSwitchState.objects.get_or_create(pk=1)

        if not state.is_active:
            self.stdout.write(self.style.WARNING("Kill switch is not currently active -- nothing to do."))
            return

        self.stdout.write(
            f"Kill switch was activated at {state.activated_at} "
            f"(triggering event: {state.triggered_by_event})."
        )

        if not options["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "Dry run only -- re-run with --confirm to actually deactivate the kill switch. "
                    "Before doing that: have you actually understood and addressed whatever tripped it?"
                )
            )
            return

        if not options["by"].strip():
            raise CommandError("--by cannot be blank -- the audit trail needs to know who did this.")

        state.is_active = False
        state.deactivated_at = timezone.now()
        state.deactivated_by = options["by"]
        state.save(update_fields=["is_active", "deactivated_at", "deactivated_by"])

        log_action(
            action="kill_switch_rearmed",
            actor_label=f"management_command:{options['by']}",
            target=state,
            details={"deactivated_by": options["by"]},
        )

        self.stdout.write(self.style.SUCCESS(f"Kill switch deactivated by {options['by']}."))
