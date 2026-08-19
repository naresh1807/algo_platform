from rest_framework.routers import DefaultRouter

from .views import AuditLogViewSet

app_name = "admin_tools"

router = DefaultRouter()
router.register("audit-log", AuditLogViewSet, basename="audit-log")

urlpatterns = router.urls
