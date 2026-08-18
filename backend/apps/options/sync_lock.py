"""
Redis-backed distributed lock guarding contract/instrument-master sync
-- so Celery Beat's scheduled tasks, a manually-run `sync_option_contracts`
command, and multiple Celery workers can never run the same underlying's
sync concurrently (which would otherwise let two processes both
download/parse/upsert at once and race on the same OptionContract rows).

Uses the `redis` package directly (already an installed dependency --
Celery's own broker/result backend and Django Channels' channel layer
both already require it, see config/settings.py's CELERY_BROKER_URL/
CHANNEL_LAYERS) rather than Django's cache framework: this project has
no CACHES setting today (django.core.cache.cache defaults to the
per-process LocMemCache), and a lock that only works within one process
would defeat the entire point -- adding a project-wide Redis cache
backend just to get a lock is a bigger, unrelated change than this
needs. Reuses the exact same REDIS_URL env-var convention config/
settings.py already uses for CELERY_BROKER_URL/CHANNEL_LAYERS.

Standard safe-Redis-lock pattern: SET key token NX PX ttl to acquire
(atomic -- only succeeds if the key doesn't already exist), and a
Lua script to release that only deletes the key if it still holds OUR
token (so a lock we merely THINK we hold -- e.g. because it already
expired and someone else acquired it in the meantime -- can never be
released out from under its new, legitimate holder).
"""

from __future__ import annotations

import contextlib
import logging
import os
import uuid

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_client: "redis.Redis | None" = None


def _redis_client() -> "redis.Redis":
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        _client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
    return _client


def acquire_lock(key: str, timeout: int | None = None) -> str | None:
    """
    Attempts to acquire `key`; returns a unique token on success (pass
    it to release_lock) or None if someone else already holds it.
    `timeout` (seconds) defaults to settings.OPTIONS_SYNC_LOCK_TIMEOUT_SECONDS
    -- the lock self-expires even if the holder crashes without
    releasing it, so a dead process can never wedge sync forever.
    """
    timeout = timeout or settings.OPTIONS_SYNC_LOCK_TIMEOUT_SECONDS
    token = uuid.uuid4().hex
    try:
        acquired = _redis_client().set(key, token, nx=True, ex=timeout)
    except redis.RedisError:
        # Redis being unreachable must not be what breaks contract sync
        # outright (BROKER_MODE=paper/local-dev installs may not always
        # have Redis up) -- logged loudly, then treated as "couldn't
        # acquire," the same safe-by-default posture as everything else
        # in this codebase that degrades rather than crashing when an
        # external dependency is unavailable.
        logger.exception("sync_lock: Redis unreachable while acquiring %r -- treating as not acquired.", key)
        return None
    return token if acquired else None


def release_lock(key: str, token: str) -> None:
    """No-op (logged) if Redis is unreachable or the lock already expired/was never held by us -- release is always safe to call."""
    try:
        _redis_client().eval(_RELEASE_SCRIPT, 1, key, token)
    except redis.RedisError:
        logger.exception("sync_lock: Redis unreachable while releasing %r.", key)


@contextlib.contextmanager
def sync_lock(key: str, timeout: int | None = None):
    """
    Usage:
        with sync_lock(f"options:sync:{underlying}") as acquired:
            if not acquired:
                return {"skipped": True, "reason": "sync_already_in_progress"}
            ... do the sync ...
    Always releases on the way out (success or exception) if acquired;
    never blocks waiting for the lock -- callers (Celery tasks, the
    management command) are expected to skip/return rather than queue,
    since a sync that's already running will cover the same work anyway.
    """
    token = acquire_lock(key, timeout=timeout)
    try:
        yield token is not None
    finally:
        if token is not None:
            release_lock(key, token)
