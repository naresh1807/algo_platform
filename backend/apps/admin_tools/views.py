from rest_framework import viewsets

from common.permissions import IsAdminGroup

from .models import AuditLog
from .serializers import AuditLogSerializer


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Admin-only, read-only -- the whole point of an audit log is that it
    cannot be edited or deleted through the normal API (manual section
    16: auditability requires the log itself be trustworthy). Nothing
    in this codebase writes to AuditLog except apps.admin_tools.audit.log_action.
    """
    queryset = AuditLog.objects.select_related("actor").all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminGroup]
    filterset_fields = ["action", "target_type"]
