from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from common.authentication import ExpiringTokenAuthentication


class LoginRateThrottle(SimpleRateThrottle):
    """Rate-limit login attempts by client address even if a token is supplied."""

    scope = "login"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class RotatingAuthTokenView(ObtainAuthToken):
    """Issue a fresh bounded-lifetime token on every successful login."""

    throttle_classes = [LoginRateThrottle]

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        authenticated_user = serializer.validated_data["user"]
        User = get_user_model()
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=authenticated_user.pk)
            Token.objects.filter(user=user).delete()
            token = Token.objects.create(user=user)
        return Response({"token": token.key})


class LogoutView(APIView):
    """Revoke the exact DRF token used to authenticate this request."""

    authentication_classes = [ExpiringTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.auth.delete()
        return Response(status=204)
