from django.urls import path

from .views import LogoutView, RotatingAuthTokenView

app_name = "auth"

urlpatterns = [
    path("token/", RotatingAuthTokenView.as_view(), name="api-token-auth"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
