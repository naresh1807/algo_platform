from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("apps.auth_app.urls")),
    path("api/market-data/", include("apps.market_data.urls")),
    path("api/news/", include("apps.news.urls")),
    path("api/options/", include("apps.options.urls")),
    path("api/signals/", include("apps.signals.urls")),
    path("api/risk/", include("apps.risk.urls")),
    path("api/execution/", include("apps.execution.urls")),
    path("api/learning/", include("apps.learning.urls")),
    path("api/monitoring/", include("apps.monitoring.urls")),
    path("api/analytics/", include("apps.analytics.urls")),
    path("api/admin-tools/", include("apps.admin_tools.urls")),
    path("api/jarvis/", include("apps.jarvis.urls")),
    path("api/investing/", include("apps.investing.urls")),
]
