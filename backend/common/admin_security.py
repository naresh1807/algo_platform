"""
Rate-limits POST requests to the Django admin login page by client IP.

Django's built-in admin login (config/urls.py: path("admin/", admin.site.urls))
has NO brute-force protection of its own -- apps.auth_app.views.
RotatingAuthTokenView's LoginRateThrottle only wraps DRF views, so it
never sees requests to this plain Django view at all. A brute-forced
admin login is a bigger prize than the API login: common.permissions.
_in_group short-circuits every RBAC check for a superuser
(`if user.is_superuser: return True`), so a successful admin brute-force
bypasses the Trader/Admin group distinction entirely, not just this
one endpoint.

Reuses Django's cache framework (the same Redis-backed default cache
apps.auth_app's own throttle sits on top of via DRF) and the same
rate settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["login"] already
defines, rather than inventing a second, independent policy to keep in
sync.

IP determination deliberately mirrors this project's own documented
"NUM_PROXIES=0 is the safe default: use REMOTE_ADDR and do not trust a
spoofable X-Forwarded-For header" stance (config/settings.py) --
REMOTE_ADDR only. A deployment that sits behind trusted proxies needs
the same proxy-count-aware handling DRF's own throttle has; this stays
intentionally simple rather than half-replicating that logic.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse

# Matches config/urls.py's actual mount point (path("admin/", admin.site.urls)
# -> Django's AdminSite always serves its login view at "<prefix>/login/").
# If that prefix is ever changed, this must change with it.
ADMIN_LOGIN_PATH = "/admin/login/"

_PERIOD_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_rate(rate: str) -> tuple[int, int]:
    """'5/minute' -> (5, 60) -- same format DRF's SimpleRateThrottle.parse_rate accepts."""
    num, period = rate.split("/")
    return int(num), _PERIOD_SECONDS[period[0].lower()]


class AdminLoginRateLimitMiddleware:
    """Blocks admin-login POSTs from a client IP once it exceeds the
    configured login rate, independent of whether any individual
    attempt's credentials were valid -- credential correctness is
    irrelevant to why this exists (a brute-force script sends many
    invalid attempts; this must trip before Django even evaluates one)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path == ADMIN_LOGIN_PATH:
            # Read the rate fresh on every request (not cached at __init__,
            # which Django only calls once at process startup) -- both so
            # a config change takes effect without a process restart, and
            # so @override_settings actually has any effect in tests.
            rate = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}).get("login", "5/minute")
            limit, window_seconds = _parse_rate(rate)

            ip = request.META.get("REMOTE_ADDR") or "unknown"
            cache_key = f"admin_login_throttle:{ip}"
            attempts = cache.get(cache_key, 0)
            if attempts >= limit:
                return HttpResponse(
                    "Too many admin login attempts. Try again later.", status=429,
                )
            # Counted per POST attempt, not per success/failure -- see
            # class docstring. add() only sets the initial TTL on the
            # FIRST attempt in a window; incr() on an already-present
            # key preserves its existing expiry rather than resetting
            # the window on every attempt, which would let a slow-and-
            # steady attacker never actually get throttled.
            cache.add(cache_key, 0, timeout=window_seconds)
            try:
                cache.incr(cache_key)
            except ValueError:
                cache.set(cache_key, 1, timeout=window_seconds)

        return self.get_response(request)
