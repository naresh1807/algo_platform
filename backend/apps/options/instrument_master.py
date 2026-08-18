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


def _download() -> list[dict]:
    logger.info("Downloading Angel One instrument master from %s", INSTRUMENT_MASTER_URL)
    response = requests.get(INSTRUMENT_MASTER_URL, timeout=90)
    response.raise_for_status()
    return response.json()


def get_instrument_master(force_refresh: bool = False) -> list[dict]:
    """
    Returns the FULL instrument list (every exchange/instrument type),
    cached in-process for _CACHE_TTL_SECONDS. Deliberately unfiltered
    here -- different callers need different subsets (this module's own
    options_for_expiry below; a future equity/futures lookup elsewhere),
    so filtering is left to each caller rather than baked into the fetch.
    """
    now = time.time()
    if force_refresh or _cache["data"] is None or (now - _cache["fetched_at"]) > _CACHE_TTL_SECONDS:
        _cache["data"] = _download()
        _cache["fetched_at"] = now
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
