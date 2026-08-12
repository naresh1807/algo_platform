"""
BSE India's public (unofficial) JSON API -- closes the gap
apps.investing.tasks.sync_index_constituents_and_prices's own
docstring flagged: NSE's API (fundamentals_client.NSEDataClient) has
zero BSE coverage, so SENSEX (and any other BSE-only index) could
exist as an Index row but never actually sync.

Same honesty posture as NSEDataClient and every broker integration in
this codebase (apps/options/broker_client.py, apps/execution/
live_executor.py): real, working request/parse logic below, NOT
fabricated data -- but THIS HAS NOT BEEN VERIFIED AGAINST A LIVE BSE
SESSION FROM THIS SANDBOX (no network access here). BSE's public API
is even less commonly documented by independent tools than NSE's is,
so treat the endpoint paths and field names below as the least-
verified part of this entire codebase -- check the actual response
shape against a real session before trusting the sync task to
populate anything from it.

Known real constraint (not a guess -- BSE's site works this way):
unlike NSE, BSE's api.bseindia.com subdomain generally does NOT
require the same homepage-cookie bootstrap NSE's www.nseindia.com
does -- but it DOES require an Origin/Referer header matching
bseindia.com, or requests get rejected. No session bootstrap step is
needed here for that reason; if that turns out to be wrong for a
particular endpoint, add one following NSEDataClient._bootstrap_session's
exact pattern.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bseindia.com/BseIndiaAPI/api"
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}


class BSEDataClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(_DEFAULT_HEADERS)

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        response = self.session.get(f"{BASE_URL}{path}", params=params, timeout=10)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            text_preview = (response.text or "")[:500]
            logger.exception(
                "BSEDataClient: failed to parse JSON for %s. Response preview: %r",
                path,
                text_preview,
            )
            return None

    def get_sensex_snapshot(self) -> dict | None:
        """
        SENSEX's own current level + change. BSE's index-data endpoint
        shape -- see module docstring's caveat, this is the least
        verified call in the whole codebase. Returns None on failure.
        """
        try:
            return self._get("/SensexData/w")
        except requests.RequestException:
            logger.exception("BSEDataClient.get_sensex_snapshot failed")
            return None

    def get_sensex_constituents(self) -> list[dict]:
        """
        SENSEX's 30 constituent scrips with current price/change.
        Separate call from get_sensex_snapshot -- unlike NSE's
        single-call equity-stockIndices response (index total +
        members together), BSE's public API appears to split these
        (again, unverified -- adjust if the real response disagrees).
        """
        try:
            data = self._get("/SensexConstituents/w")
            return data if isinstance(data, list) else (data or {}).get("Table", [])
        except requests.RequestException:
            logger.exception("BSEDataClient.get_sensex_constituents failed")
            return []
