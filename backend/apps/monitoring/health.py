"""
Aggregated platform health -- GET /api/monitoring/health/. One call
covering the pieces this platform's own incident history has actually
failed in isolation: Django up but MySQL down, Redis reachable but the
priority Celery worker never started (fix-list item 7), the live-feed
process not running at all, or an option feed that is technically
"connected" while its ticks have gone stale (the tick-drop incident --
see apps.market_data.broker_ws_client's own module docstring).

Every check here is cheap and non-blocking: MySQL/Redis checks are
trivial local connectivity probes, and every live-feed/Celery-worker
field is a pre-published read from cache (apps.market_data.feed_stats /
the heartbeat tasks below) -- never a live RPC into another process,
and never a broker call (this endpoint must be safe to poll from the
frontend on an interval without ever contributing to Angel One
rate-limit pressure). Never returns credentials or any raw
settings/environment value.
"""

from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsTraderOrAdmin

# Written by apps.monitoring.tasks.heartbeat_default_worker /
# heartbeat_priority_worker (config/celery.py beat_schedule) -- one key
# per queue, so a stale key means "nothing on that queue has run
# recently," which is exactly what fix-list item 7 (priority queue
# silently not being consumed) needs to be observable.
_CELERY_HEARTBEAT_KEYS = {
    "default": "celery:heartbeat:celery",
    "priority": "celery:heartbeat:priority",
}


def _check_mysql() -> dict:
    try:
        connection.ensure_connection()
        return {"ok": True, "detail": ""}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:300]}


def _check_redis() -> dict:
    try:
        cache.set("monitoring:health:ping", "1", timeout=5)
        ok = cache.get("monitoring:health:ping") == "1"
        return {"ok": ok, "detail": "" if ok else "cache round-trip did not return the expected value"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:300]}


def _parse_iso(raw) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parse_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed


def _staleness(at: datetime | None, stale_after_seconds: int | None) -> dict:
    """`stale_after_seconds=None` means "don't expect fresh data right now" (e.g. market closed) -- never flagged stale in that case."""
    if at is None:
        return {"at": None, "stale": stale_after_seconds is not None, "seconds_ago": None}
    seconds_ago = (timezone.now() - at).total_seconds()
    stale = stale_after_seconds is not None and seconds_ago > stale_after_seconds
    return {"at": at.isoformat(), "stale": stale, "seconds_ago": round(seconds_ago, 1)}


def _celery_worker_heartbeat(queue: str) -> dict:
    raw = cache.get(_CELERY_HEARTBEAT_KEYS[queue])
    return _staleness(_parse_iso(raw), settings.CELERY_HEARTBEAT_STALE_SECONDS)


class SystemHealthView(APIView):
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from apps.market_data import feed_stats
        from apps.market_data.market_hours import is_market_open
        from apps.options.models import OptionChainSnapshot
        from apps.options.subscription_manager import resolve_live_expiry

        market_open, market_reason = is_market_open()
        stale_after = settings.LIVE_FEED_STALE_SECONDS if market_open else None

        live = feed_stats.read_status()
        heartbeat = _staleness(_parse_iso(live["heartbeat_at"]), settings.LIVE_FEED_STALE_SECONDS)
        index_tick = _staleness(_parse_iso(live["last_index_tick_at"]), stale_after)
        option_tick = _staleness(_parse_iso(live["last_option_tick_at"]), stale_after)

        last_snapshot_at = (
            OptionChainSnapshot.objects.order_by("-timestamp").values_list("timestamp", flat=True).first()
        )

        selected_expiries = {}
        for underlying in settings.OPTIONS_PIPELINE_UNDERLYINGS:
            underlying = underlying.strip()
            if not underlying:
                continue
            expiry = resolve_live_expiry(underlying)
            selected_expiries[underlying] = expiry.isoformat() if expiry else None

        celery_default = _celery_worker_heartbeat("default")
        celery_priority = _celery_worker_heartbeat("priority")

        body = {
            "timestamp": timezone.now().isoformat(),
            "django": {"ok": True, "detail": ""},
            "mysql": _check_mysql(),
            "redis": _check_redis(),
            "celery_default_worker": celery_default,
            "celery_priority_worker": celery_priority,
            # Celery Beat has no observable effect other than scheduling
            # a task for a worker to run -- there is no separate signal
            # that proves "Beat itself ticked" independent of a worker
            # also being alive to consume what it scheduled. Reusing the
            # default-worker heartbeat here is an honest choice, not a
            # blind guess: if Beat were down, that heartbeat (which Beat
            # itself must schedule every cycle) would go stale within
            # the same window.
            "celery_beat": celery_default,
            "market_open": market_open,
            "market_status_detail": market_reason,
            "live_feed": {
                "process_heartbeat": heartbeat,
                "connection_state": live["connection_state"],
                "last_index_tick": index_tick,
                "last_option_tick": option_tick,
                "subscribed_option_token_count": live["subscribed_option_token_count"],
                "tick_stats": live["option_tick_stats"],
                "index_tick_stats": live["index_tick_stats"],
                "last_error": live["last_error"],
            },
            "selected_option_expiry": selected_expiries,
            "last_successful_option_snapshot": last_snapshot_at.isoformat() if last_snapshot_at else None,
        }
        return Response(body)
