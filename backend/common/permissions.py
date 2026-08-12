"""
manual section 16: "Role-based access control" / "Separate admin and
trading privileges" / "Manual approval for high-risk changes".

Two groups (set up via the setup_rbac_groups management command):
  - "Admin": can promote/demote strategy versions, approve daily
    reviews, view the audit log -- the governance-level actions.
  - "Trader": can use the read-only dashboard (view signals, positions,
    risk state, options analytics) but CANNOT touch StrategyVersion or
    approve a DailyReviewNote.

Deliberately just two groups, not a fine-grained permission-per-action
system -- this is a personal/small-team platform (manual section 1),
and two roles map directly onto the manual's own phrase "separate
admin and trading privileges" without inventing distinctions the
manual never asked for. A Django superuser always passes both checks
regardless of group membership (is_superuser is the standard Django
escape hatch for "the person who can fix anything if the RBAC setup
itself is broken").
"""

from rest_framework.permissions import BasePermission

ADMIN_GROUP_NAME = "Admin"
TRADER_GROUP_NAME = "Trader"


def _in_group(user, group_name: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=group_name).exists()


class IsAdminGroup(BasePermission):
    """
    For governance-level writes: promoting a StrategyVersion,
    approving a DailyReviewNote, viewing the AuditLog. A user who is
    only in "Trader" (or in no group at all) is rejected here even
    though they're authenticated -- this is what actually enforces
    "separate admin and trading privileges", not just documents it.
    """

    message = "This action requires Admin privileges."

    def has_permission(self, request, view):
        return _in_group(request.user, ADMIN_GROUP_NAME)


class IsTraderOrAdmin(BasePermission):
    """
    For the ordinary read-only dashboard surface -- both roles are
    allowed, since a Trader still needs to see signals/positions/risk
    state to do their job; they just can't govern the strategy itself.
    """

    def has_permission(self, request, view):
        return _in_group(request.user, TRADER_GROUP_NAME) or _in_group(request.user, ADMIN_GROUP_NAME)
