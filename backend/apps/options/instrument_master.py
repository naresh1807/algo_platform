"""
Downloads and caches Angel One's instrument master -- the file every
SmartAPI integration needs for symbol->token lookups it doesn't already
know statically (apps.market_data.broker_client.SYMBOL_TOKENS is a tiny
hand-maintained map good enough for 2 index spot symbols; option chains
need 80+ contract tokens per expiry across a rolling set of expiries,
which is exactly what this file is for).

Source: Angel One publishes this at a public, unauthenticated URL
(confirmed against SmartAPI's own forum/docs as of this writing):
    https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json
It's large (every tradable instrument across every exchange Angel One
supports -- tens of MB), republished roughly daily, so this module
caches it in-process with a TTL instead of re-downloading per request.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

import requests

logger = logging.getLogger(__name__)

INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

_CACHE_TTL_SECONDS = 24 * 60 * 60  # republished ~daily; no need to refresh more often
_cache: dict = {"data": None, "fetched_at": 0.0}

_DOWNLOAD_MAX_ATTEMPTS = 4
_DOWNLOAD_BASE_BACKOFF_SECONDS = 2.0  # attempt N waits BASE * 2**(N-1): 2s, 4s, 8s

# A handful of representative index-option rows this file's OTHER
# functions (options_for_expiry/list_expiries) actually depend on --
# checked before a download is allowed to replace the cache, so a
# truncated/malformed/wrong-shape response (a bad CDN response, an
# HTML error page Angel One served with a 200 status, etc.) can never
# silently poison contract sync. Deliberately loose (structural checks
# only, not exact values) -- this is a sanity check, not a schema
# validator for a file this codebase doesn't control the format of.
_REQUIRED_ROW_KEYS = {"token", "symbol", "name", "expiry", "instrumenttype", "exch_seg", "strike"}


class InstrumentMasterError(Exception):
    """Raised when the instrument master can't be downloaded or fails validation -- never let a caller mistake this for 'legitimately empty'."""


def _validate_master(data) -> list[dict]:
    if not isinstance(data, list) or len(data) == 0:
        raise InstrumentMasterError(
            f"Instrument master response was not a non-empty list (got {type(data).__name__})."
        )
    # Scans the FULL list for a sample of OPTIDX rows to check their
    # shape (options_for_expiry/list_expiries' actual read pattern) --
    # NOT capped to the first N entries: this file is not documented or
    # guaranteed to be sorted by instrumenttype/exchange, and an early
    # draft of this check that only looked at the first 2000 entries
    # produced real false positives against the live file (confirmed:
    # apps.investing's own real-network test hit exactly this). A full
    # scan is still cheap relative to the download itself (get_instrument_
    # master already caches the validated result for 24h, so this only
    # runs once per day per process in practice, not once per request).
    sample = [row for row in data if row.get("instrumenttype") == "OPTIDX"][:20]
    if not sample:
        raise InstrumentMasterError(
            "Instrument master response has no recognizable OPTIDX rows anywhere -- "
            "response shape may have changed or this is a malformed/error payload."
        )
    for row in sample:
        missing = _REQUIRED_ROW_KEYS - row.keys()
        if missing:
            raise InstrumentMasterError(f"Instrument master OPTIDX row missing expected key(s): {sorted(missing)}.")
    return data


def _download() -> list[dict]:
    """
    Retries with exponential backoff on network/HTTP/JSON failures --
    this file is tens of MB over a public, unauthenticated, third-party
    URL, so a transient timeout/5xx must not permanently break contract
    sync until the next scheduled attempt. Raises InstrumentMasterError
    (not requests' own exception types) after exhausting attempts, so
    every caller has one exception type to catch regardless of which
    underlying network failure actually happened.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            logger.info(
                "Downloading Angel One instrument master from %s (attempt %d/%d)",
                INSTRUMENT_MASTER_URL, attempt, _DOWNLOAD_MAX_ATTEMPTS,
            )
            response = requests.get(INSTRUMENT_MASTER_URL, timeout=90)
            response.raise_for_status()
            data = response.json()
            return _validate_master(data)
        except (requests.RequestException, ValueError, InstrumentMasterError) as exc:
            last_exc = exc
            if attempt < _DOWNLOAD_MAX_ATTEMPTS:
                backoff = _DOWNLOAD_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Instrument master download attempt %d/%d failed (%s) -- retrying in %.0fs.",
                    attempt, _DOWNLOAD_MAX_ATTEMPTS, exc, backoff,
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "Instrument master download failed after %d attempts: %s", _DOWNLOAD_MAX_ATTEMPTS, exc,
                )
    raise InstrumentMasterError(f"Instrument master download failed after {_DOWNLOAD_MAX_ATTEMPTS} attempts: {last_exc}") from last_exc


def get_instrument_master(force_refresh: bool = False) -> list[dict]:
    """
    Returns the FULL instrument list (every exchange/instrument type),
    cached in-process for _CACHE_TTL_SECONDS. Deliberately unfiltered
    here -- different callers need different subsets (this module's own
    options_for_expiry below; a future equity/futures lookup elsewhere),
    so filtering is left to each caller rather than baked into the fetch.

    On a failed refresh, the existing (already-validated) cached copy is
    kept and served rather than raised/emptied -- "stale but real" beats
    "no data at all" for every current caller, and callers that need to
    know sync freshness read apps.options.models.OptionSyncStatus
    (populated by apps.options.contract_sync) instead of inferring it
    from this in-process cache, which doesn't survive a process restart
    anyway. force_refresh=True with no usable cache yet still raises
    InstrumentMasterError -- there is nothing safe to fall back to.
    """
    now = time.time()
    is_stale = _cache["data"] is None or (now - _cache["fetched_at"]) > _CACHE_TTL_SECONDS
    if force_refresh or is_stale:
        try:
            _cache["data"] = _download()
            _cache["fetched_at"] = now
        except InstrumentMasterError:
            if _cache["data"] is not None:
                logger.warning("Instrument master refresh failed -- serving the last validated cached copy instead.")
                return _cache["data"]
            raise
    return _cache["data"]


def find_index_token(display_name: str, exch_seg: str) -> dict | None:
    """
    Looks up an INDEX's (not an option contract's) Angel One token by
    name -- e.g. find_index_token("NIFTY AUTO", "NSE") ->
    {"tradingsymbol": "Nifty Auto", "token": "99926029"}.

    Added for apps.investing's index-price sync (see that app's
    tasks.py): apps.investing.fundamentals_client's NSE scraper is
    blocked by nseindia.com's Akamai bot protection (confirmed
    2026-08-08 -- returns a blocked/404 challenge page to plain
    `requests` calls, even with a real browser User-Agent and cookie
    bootstrap), but this instrument master already has real, working
    tokens for every index this platform tracks -- no separate
    hand-maintained token map needed the way
    apps.market_data.broker_client.SYMBOL_TOKENS is for the 2-symbol
    options watchlist.

    Matches on Angel One's own `symbol` field (e.g. "Nifty 50", "Nifty
    Auto") case-insensitively against display_name, scoped to
    instrumenttype == "AMXIDX" (index rows) on the given exchange --
    Angel One's `name` field for indices is inconsistent ("NIFTY" for
    Nifty 50 but "NIFTY IT" spelled out for others), so `symbol` is the
    more reliable match target. Returns None if no match, same
    "missing lookup is normal, not a crash" convention as the rest of
    this module.
    """
    master = get_instrument_master()
    target = display_name.strip().lower()
    for row in master:
        if row.get("exch_seg") != exch_seg or row.get("instrumenttype") != "AMXIDX":
            continue
        if (row.get("symbol") or "").strip().lower() == target:
            return {"tradingsymbol": row["symbol"], "token": row["token"]}
    return None


def _parse_expiry(expiry_str: str) -> date | None:
    """Angel One's expiry format in this file is DDMMMYYYY, e.g. '28AUG2025'."""
    if not expiry_str:
        return None
    try:
        return datetime.strptime(expiry_str, "%d%b%Y").date()
    except ValueError:
        return None


def list_expiries(underlying: str, limit: int = 12) -> list[date]:
    """
    Every distinct expiry currently listed for this underlying's index
    options, nearest first -- used by sync_option_contracts --list-expiries
    (and could back a future "available expiries" API endpoint) so
    the expiry to sync doesn't have to be guessed/typed blindly.
    """
    master = get_instrument_master()
    expiries = set()
    for row in master:
        if row.get("exch_seg") != "NFO" or row.get("instrumenttype") != "OPTIDX":
            continue
        if row.get("name") != underlying:
            continue
        parsed = _parse_expiry(row.get("expiry", ""))
        if parsed:
            expiries.add(parsed)
    return sorted(expiries)[:limit]


def options_for_expiry(underlying: str, expiry: date) -> list[dict]:
    """
    Filters the instrument master down to every NFO index-option
    contract for one underlying+expiry. Returns [{"strike",
    "option_type", "symbol_token", "tradingsymbol", "lot_size"}, ...] --
    exactly the shape apps.options.tasks needs to create/update
    OptionContract rows. tradingsymbol/lot_size are what an actual order
    placement needs beyond the quote-only symbol_token (see
    OptionContract's own field docstrings).

    Scoped to instrumenttype == "OPTIDX" (index options: NIFTY,
    BANKNIFTY, etc.) since that matches this scaffold's current
    index-only watchlist (apps.market_data.broker_client.SYMBOL_TOKENS).
    Single-stock options use instrumenttype "OPTSTK" in the same file --
    add that if/when stock options are needed too.
    """
    master = get_instrument_master()
    results = []
    for row in master:
        if row.get("exch_seg") != "NFO" or row.get("instrumenttype") != "OPTIDX":
            continue
        if row.get("name") != underlying:
            continue
        if _parse_expiry(row.get("expiry", "")) != expiry:
            continue

        symbol = row.get("symbol", "")
        if symbol.endswith("CE"):
            option_type = "CE"
        elif symbol.endswith("PE"):
            option_type = "PE"
        else:
            continue  # not a plain CE/PE contract (shouldn't happen for OPTIDX, but don't guess)

        try:
            # Angel One stores strike as price*100, as a string (confirmed
            # on the SmartAPI forum: "strike":"1750000.000000" == 17500.00).
            strike = float(row["strike"]) / 100
        except (KeyError, ValueError, TypeError):
            continue

        results.append({
            "strike": strike,
            "option_type": option_type,
            "symbol_token": row["token"],
            "tradingsymbol": symbol,
            "lot_size": int(row.get("lotsize") or 0),
        })
    return results
