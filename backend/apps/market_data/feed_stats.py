"""
Cross-process live-feed observability.

apps.market_data.management.commands.run_live_feed runs as its own OS
process -- entirely separate from the Django web process that serves
apps.monitoring's health endpoint. There is no RPC/socket between them
(and there shouldn't be one, for a single long-running background
process like this), so run_live_feed publishes small, cheap status
values into the same Redis-backed django.core.cache every other
cross-process concern in this codebase already uses (see
apps.options.sync_lock's own docstring for the same reasoning applied
to distributed locking) and the health endpoint just reads them back.

Every key has a short TTL (see _TIMEOUT below): if run_live_feed dies
or is never started, its keys simply expire and read back as None/empty
-- the health endpoint reports "no heartbeat" honestly instead of
serving a stale "last known good" value forever.
"""

from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone

# Longer than the subscription-refresh/heartbeat cadence
# (settings.OPTIONS_LIVE_SUBSCRIPTION_REFRESH_SECONDS) so a normal gap
# between writes never makes a healthy feed look stale, short enough
# that a genuinely dead process is reported as such within a couple of
# minutes, not indefinitely.
_TIMEOUT = 120

_KEY_HEARTBEAT = "livefeed:heartbeat_at"
_KEY_LAST_INDEX_TICK = "livefeed:last_index_tick_at"
_KEY_LAST_OPTION_TICK = "livefeed:last_option_tick_at"
_KEY_OPTION_STATS = "livefeed:option_tick_stats"
_KEY_INDEX_STATS = "livefeed:index_tick_stats"
_KEY_SUBSCRIBED_TOKENS = "livefeed:subscribed_option_token_count"
_KEY_CONNECTION_STATE = "livefeed:connection_state"
_KEY_LAST_ERROR = "livefeed:last_error"


def mark_heartbeat() -> None:
    cache.set(_KEY_HEARTBEAT, timezone.now().isoformat(), timeout=_TIMEOUT)


def mark_index_tick() -> None:
    cache.set(_KEY_LAST_INDEX_TICK, timezone.now().isoformat(), timeout=_TIMEOUT)


def mark_option_tick() -> None:
    cache.set(_KEY_LAST_OPTION_TICK, timezone.now().isoformat(), timeout=_TIMEOUT)


def set_option_tick_stats(stats: dict) -> None:
    cache.set(_KEY_OPTION_STATS, stats, timeout=_TIMEOUT)


def set_index_tick_stats(stats: dict) -> None:
    cache.set(_KEY_INDEX_STATS, stats, timeout=_TIMEOUT)


def set_subscribed_token_count(count: int) -> None:
    cache.set(_KEY_SUBSCRIBED_TOKENS, count, timeout=_TIMEOUT)


def set_connection_state(state: str) -> None:
    """state: one of 'connecting' | 'connected' | 'reconnecting' | 'market_closed' | 'stopped'."""
    cache.set(_KEY_CONNECTION_STATE, state, timeout=_TIMEOUT)


def record_error(category: str, detail: str = "") -> None:
    cache.set(
        _KEY_LAST_ERROR,
        {"category": category, "detail": (detail or "")[:500], "at": timezone.now().isoformat()},
        # Kept longer than the other keys -- the LAST error stays
        # informative for a while even after the feed recovers, rather
        # than vanishing the moment a healthy tick arrives.
        timeout=_TIMEOUT * 6,
    )


def read_status() -> dict:
    """Everything apps.monitoring.health.SystemHealthView needs from the live-feed process, in one call."""
    return {
        "heartbeat_at": cache.get(_KEY_HEARTBEAT),
        "last_index_tick_at": cache.get(_KEY_LAST_INDEX_TICK),
        "last_option_tick_at": cache.get(_KEY_LAST_OPTION_TICK),
        "option_tick_stats": cache.get(_KEY_OPTION_STATS) or {},
        "index_tick_stats": cache.get(_KEY_INDEX_STATS) or {},
        "subscribed_option_token_count": cache.get(_KEY_SUBSCRIBED_TOKENS),
        "connection_state": cache.get(_KEY_CONNECTION_STATE),
        "last_error": cache.get(_KEY_LAST_ERROR),
    }
