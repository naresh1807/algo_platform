from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("apps.auth_app.urls", namespace="auth")),
    path("api/market-data/", include("apps.market_data.urls", namespace="market_data")),
    path("api/news/", include("apps.news.urls", namespace="news")),
    path("api/options/", include("apps.options.urls", namespace="options")),
    path("api/signals/", include("apps.signals.urls", namespace="signals")),
    path("api/risk/", include("apps.risk.urls", namespace="risk")),
    path("api/execution/", include("apps.execution.urls", namespace="execution")),
    path("api/learning/", include("apps.learning.urls", namespace="learning")),
    path("api/monitoring/", include("apps.monitoring.urls", namespace="monitoring")),
    path("api/analytics/", include("apps.analytics.urls", namespace="analytics")),
    path("api/admin-tools/", include("apps.admin_tools.urls", namespace="admin_tools")),
    path("api/jarvis/", include("apps.jarvis.urls", namespace="jarvis")),
    path("api/investing/", include("apps.investing.urls", namespace="investing")),
]
