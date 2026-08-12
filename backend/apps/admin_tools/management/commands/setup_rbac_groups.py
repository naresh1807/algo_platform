"""
One-time (idempotent) setup of the two RBAC groups
(common/permissions.py's ADMIN_GROUP_NAME/TRADER_GROUP_NAME). Safe to
re-run -- get_or_create means running this twice doesn't duplicate
groups or reset who's already been added to them.

Usage:
    python manage.py setup_rbac_groups
    python manage.py setup_rbac_groups --add-admin yourusername
    python manage.py setup_rbac_groups --add-trader someoneelse
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from common.permissions import ADMIN_GROUP_NAME, TRADER_GROUP_NAME


class Command(BaseCommand):
    help = "Create the Admin/Trader RBAC groups, and optionally add a user to one."

    def add_arguments(self, parser):
        parser.add_argument("--add-admin", help="Username to add to the Admin group.")
        parser.add_argument("--add-trader", help="Username to add to the Trader group.")

    def handle(self, *args, **options):
        admin_group, created = Group.objects.get_or_create(name=ADMIN_GROUP_NAME)
        self.stdout.write(f"'{ADMIN_GROUP_NAME}' group: {'created' if created else 'already existed'}")

        trader_group, created = Group.objects.get_or_create(name=TRADER_GROUP_NAME)
        self.stdout.write(f"'{TRADER_GROUP_NAME}' group: {'created' if created else 'already existed'}")

        User = get_user_model()

        if options["add_admin"]:
            user = self._find_user(User, options["add_admin"])
            user.groups.add(admin_group)
            self.stdout.write(self.style.SUCCESS(f"Added {user.username} to {ADMIN_GROUP_NAME}."))

        if options["add_trader"]:
            user = self._find_user(User, options["add_trader"])
            user.groups.add(trader_group)
            self.stdout.write(self.style.SUCCESS(f"Added {user.username} to {TRADER_GROUP_NAME}."))

    def _find_user(self, User, username):
        try:
            return User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"No user named '{username}' exists -- create it first (e.g. createsuperuser).")
