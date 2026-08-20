"""
Resolves WHICH option contracts the live Angel One WebSocket feed should
be subscribed to right now, and owns the small amount of cross-process
state (selected expiry per underlying) that decision depends on.

This is the fix for the platform's root-cause tick-drop incident: the
old apps.market_data.management.commands.run_live_feed._load_option_tokens
subscribed to EVERY synced OptionContract row for every configured
underlying -- every strike, across every one of the
settings.OPTIONS_EXPIRY_SYNC_COUNT synced expiries (6 by default), all
at once. With ~50-80 strikes x 2 sides x 6 expiries x 2 underlyings,
that is 1000+ live SNAP_QUOTE subscriptions, most of them for expiries
nobody is even looking at -- exactly what overwhelmed the single 4000-
slot option tick queue (apps.market_data.broker_ws_client) and produced
the observed 5.3M dropped ticks. This module scopes the live
subscription down to what the platform actually needs to show: the
resolved current (or operator-selected) expiry, and a bounded band of
strikes around ATM.

Historical/expired OptionContract rows are NEVER touched by this
module (no delete, no is_active mutation) -- see apps.options.
contract_sync's own docstring for why that data stays. This module only
narrows what gets a LIVE broker subscription, which is an entirely
separate concern from what stays queryable for backtesting.

Cross-process design: apps.market_data.management.commands.run_live_feed
runs as its own OS process, completely separate from the Django web
process that serves the frontend's "change expiry" request. Neither a
Python global nor an in-process signal can cross that boundary, so the
operator/frontend-selected expiry is stored in the same Redis-backed
django.core.cache every other cross-process concern in this codebase
already uses (apps.options.sync_lock's own docstring gives the same
reasoning for distributed locking) -- run_live_feed's dynamic
subscription-refresh loop (apps.market_data.broker_ws_client) polls this
module's compute_desired_option_tokens() on a short interval and reacts
to whatever it returns, with no direct RPC between the two processes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from django.conf import settings
from django.core.cache import cache

from .expiry_service import is_expiry_eligible, resolve_current_expiry
from .models import OptionContract

logger = logging.getLogger(__name__)

_SELECTED_EXPIRY_CACHE_KEY = "options:live_subscription:selected_expiry:{underlying}"
# Long enough to survive a weekend/holiday gap between sessions -- this is
# re-validated against apps.options.expiry_service on EVERY read (see
# get_selected_expiry below), never trusted blindly, so a stale cached
# value past its own expiry's cutoff just falls back to the real current
# expiry automatically rather than ever being served as-is.
_SELECTED_EXPIRY_CACHE_TIMEOUT_SECONDS = 60 * 60 * 24 * 5


def set_selected_expiry(underlying: str, expiry: date) -> None:
    """
    Records the operator/frontend-chosen expiry for `underlying` --
    called by apps.options.views.SelectedExpiryView after validating the
    request through apps.options.expiry_service (this function itself
    does NOT re-validate; callers must, same "backend is the source of
    truth" convention as apps.options.candle_service.resolve_contract).
    """
    cache.set(
        _SELECTED_EXPIRY_CACHE_KEY.format(underlying=underlying),
        expiry.isoformat(),
        timeout=_SELECTED_EXPIRY_CACHE_TIMEOUT_SECONDS,
    )


def clear_selected_expiry(underlying: str) -> None:
    cache.delete(_SELECTED_EXPIRY_CACHE_KEY.format(underlying=underlying))


def get_selected_expiry(underlying: str) -> date | None:
    """
    Returns the stored selection ONLY if it is still a real, eligible,
    actively-synced expiry -- re-validated here (not just at write time)
    so a rollover that happens while nobody is looking automatically
    stops being honored, instead of the live feed chasing a now-expired
    date until an operator manually changes the dropdown again.
    """
    raw = cache.get(_SELECTED_EXPIRY_CACHE_KEY.format(underlying=underlying))
    if not raw:
        return None
    try:
        expiry = date.fromisoformat(raw)
    except ValueError:
        return None
    if not is_expiry_eligible(expiry):
        return None
    if not OptionContract.objects.filter(underlying=underlying, expiry=expiry, is_active=True).exists():
        return None
    return expiry


def resolve_live_expiry(underlying: str) -> date | None:
    """The expiry the live feed should subscribe to right now: the operator's explicit choice if still valid, else the backend-resolved current expiry."""
    return get_selected_expiry(underlying) or resolve_current_expiry(underlying)


def _atm_strike(underlying: str):
    """Nearest listed strike to the latest real spot close, or None if either is unavailable (no candle history yet / no contracts synced)."""
    from apps.market_data.models import HistoricalData

    latest = HistoricalData.objects.filter(symbol=underlying).order_by("-timestamp").first()
    if latest is None:
        return None
    return latest.close


def desired_tokens_for_underlying(underlying: str, strike_range: int | None = None) -> dict[str, dict]:
    """
    symbol_token -> light contract identity dict, for exactly the live
    subscription this underlying should have right now: its resolved
    expiry (resolve_live_expiry above) narrowed to `strike_range`
    strikes (default settings.OPTIONS_LIVE_STRIKE_RANGE) on each side of
    ATM, both CE and PE. Returns {} (not an error) when there is no
    eligible expiry yet or no spot price to compute ATM from -- same
    "missing config/data is a skip" convention as every other
    BROKER_MODE-adjacent code path in this codebase.

    Strike range is applied against the ACTUAL listed strikes for this
    expiry (not `atm +/- N*step` arithmetic) so uneven/irregular strike
    spacing near the edges of a chain can never accidentally produce a
    gap or an out-of-range guess -- only real, synced OptionContract
    rows are ever candidates.
    """
    expiry = resolve_live_expiry(underlying)
    if expiry is None:
        return {}

    strike_range = settings.OPTIONS_LIVE_STRIKE_RANGE if strike_range is None else strike_range
    contracts_qs = OptionContract.objects.filter(underlying=underlying, expiry=expiry, is_active=True)

    if strike_range and strike_range > 0:
        atm = _atm_strike(underlying)
        strikes_sorted = sorted(set(contracts_qs.values_list("strike", flat=True)))
        if atm is not None and strikes_sorted:
            atm_index = min(range(len(strikes_sorted)), key=lambda i: abs(strikes_sorted[i] - atm))
            lo = max(0, atm_index - strike_range)
            hi = min(len(strikes_sorted), atm_index + strike_range + 1)
            allowed_strikes = set(strikes_sorted[lo:hi])
            contracts_qs = contracts_qs.filter(strike__in=allowed_strikes)
        # No spot price yet (feed just started, no HistoricalData rows):
        # fall through and subscribe the whole (already expiry-narrowed)
        # chain rather than subscribing nothing -- a live feed with no
        # option ticks at all is a worse failure mode than a temporarily
        # wider subscription until the first index tick arrives.

    tokens: dict[str, dict] = {}
    for row in contracts_qs.values("symbol_token", "id", "underlying", "expiry", "strike", "option_type"):
        tokens[row["symbol_token"]] = {
            "contract_id": row["id"],
            "underlying": row["underlying"],
            "expiry": row["expiry"].isoformat(),
            "strike": float(row["strike"]),
            "option_type": row["option_type"],
        }
    return tokens


def compute_desired_option_tokens(underlyings: list[str] | None = None) -> dict[str, dict]:
    """
    The single call apps.market_data.broker_ws_client's dynamic
    subscription-refresh loop makes on every refresh tick: symbol_token
    -> contract identity for every token that SHOULD be subscribed right
    now, across every configured underlying. Defaults to
    settings.OPTIONS_PIPELINE_UNDERLYINGS (the same underlyings contract
    sync/rollover already cover) so a new underlying added there is
    picked up automatically.
    """
    underlyings = underlyings if underlyings is not None else settings.OPTIONS_PIPELINE_UNDERLYINGS
    merged: dict[str, dict] = {}
    for underlying in underlyings:
        underlying = underlying.strip()
        if not underlying:
            continue
        try:
            merged.update(desired_tokens_for_underlying(underlying))
        except Exception:
            logger.exception("compute_desired_option_tokens: failed for underlying=%s -- leaving its tokens unchanged this cycle.", underlying)
    return merged


def make_correlation_id(prefix: str = "opt") -> str:
    """
    Angel One's SmartWebSocketV2.subscribe()/unsubscribe() document
    correlation_id as "A 10 character alphanumeric ID" -- the codebase's
    previous static IDs ("live_feed", "live_feed_options") violated both
    constraints (underscores are not alphanumeric; the second is 18
    characters). Builds a fresh <=10-char alphanumeric id per call from
    a short prefix + hex digits, purely for log/error-response
    correlation -- never parsed back by this codebase.
    """
    prefix = "".join(ch for ch in prefix if ch.isalnum())[:9] or "opt"
    suffix_len = max(1, 10 - len(prefix))
    suffix = uuid.uuid4().hex[:suffix_len]
    return (prefix + suffix)[:10]


def chunk_tokens(tokens: list[str], chunk_size: int | None = None) -> list[list[str]]:
    """Splits a token list into safe-sized batches for one subscribe()/unsubscribe() call each -- see settings.OPTIONS_LIVE_MAX_TOKENS_PER_SUBSCRIBE for why."""
    chunk_size = settings.OPTIONS_LIVE_MAX_TOKENS_PER_SUBSCRIBE if chunk_size is None else chunk_size
    chunk_size = max(1, chunk_size)
    return [tokens[i : i + chunk_size] for i in range(0, len(tokens), chunk_size)]
