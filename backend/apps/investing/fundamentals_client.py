"""
Fetches real company fundamentals from NSE India's public JSON API
(nseindia.com/api/...) -- the same UNOFFICIAL, undocumented-by-NSE
endpoints that most independent NSE data tools use, since NSE does not
publish a supported public fundamentals API. Real, working request/
parse logic below, not fabricated data -- but see the same honesty
caveat apps.options.broker_client and apps.execution.live_executor
give their own broker integrations: THIS HAS NOT BEEN VERIFIED AGAINST
A LIVE NSE SESSION FROM THIS SANDBOX (no network access here). Treat it
as a reviewed-but-unverified starting point -- run it against the real
endpoints yourself and adjust field paths if NSE's response shape has
drifted, before trusting any of its output.

Known real constraints on NSE's site (not guesses -- this is documented
behavior every NSE-scraping tool has to work around):
  - Requests without a browser-like session get blocked. `_bootstrap_session`
    hits the plain HTML homepage first to pick up the cookies NSE's
    edge/WAF requires before any /api/ call will return real data
    instead of a 401/403.
  - Cookies expire after a fairly short idle period -- a long-running
    Celery worker should re-bootstrap periodically, not once at process
    start (see _get's own retry-on-401/403 logic below).

For IPO data (sync_ipo_calendar, apps/investing/tasks.py), the same
client's get_ipo_list() targets NSE's public IPO-issues endpoint --
same unverified-endpoint-shape caveat applies there too.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.nseindia.com"
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


class NSEDataClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(_DEFAULT_HEADERS)
        self._bootstrapped = False
        self._last_bootstrap_at = None
        # seconds to keep bootstrap cookies before refreshing
        self.BOOTSTRAP_TTL_SECONDS = 300

    def _bootstrap_session(self) -> None:
        """
        NSE's WAF rejects direct /api/ calls from a fresh session --
        hitting the plain homepage first picks up the cookies real
        browsers get automatically. Idempotent-ish: call again if a
        previous call started returning 401/403 (cookies expired).
        """
        try:
            self.session.get(BASE_URL, timeout=10)
            from datetime import datetime

            self._bootstrapped = True
            self._last_bootstrap_at = datetime.utcnow()
        except requests.RequestException:
            logger.exception(
                "NSEDataClient: failed to bootstrap session against %s", BASE_URL
            )
            raise

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        # Re-bootstrap periodically to refresh cookies for long-lived
        # workers; also bootstrap if never done yet.
        from datetime import datetime

        if not self._bootstrapped or self._last_bootstrap_at is None:
            try:
                self._bootstrap_session()
            except requests.RequestException:
                logger.warning(
                    "NSEDataClient: bootstrap attempt failed; continuing to request %s",
                    path,
                )
        else:
            # TTL check
            try:
                elapsed = (datetime.utcnow() - self._last_bootstrap_at).total_seconds()
                if elapsed > self.BOOTSTRAP_TTL_SECONDS:
                    try:
                        self._bootstrap_session()
                    except requests.RequestException:
                        logger.warning(
                            "NSEDataClient: periodic bootstrap failed; continuing to request %s",
                            path,
                        )
            except Exception:
                # Defensive: if anything goes wrong computing TTL, attempt a bootstrap
                try:
                    self._bootstrap_session()
                except requests.RequestException:
                    logger.warning(
                        "NSEDataClient: bootstrap fallback failed; continuing to request %s",
                        path,
                    )

        url = f"{BASE_URL}{path}"
        response = self.session.get(url, params=params, timeout=10)
        if response.status_code in (401, 403):
            # Cookies likely expired -- one retry after re-bootstrapping,
            # not an infinite loop.
            logger.warning(
                "NSEDataClient: %s returned %s, re-bootstrapping once.",
                path,
                response.status_code,
            )
            self._bootstrap_session()
            response = self.session.get(url, params=params, timeout=10)

        # Treat 404 as a benign "no data" response rather than
        # raising; callers already handle a None/empty result.
        if response.status_code == 404:
            logger.warning("NSEDataClient: %s returned 404 (not found): %s", path, url)
            return None

        try:
            response.raise_for_status()
        except requests.HTTPError:
            logger.exception(
                "NSEDataClient: HTTP error for %s %s", path, response.status_code
            )
            return None

        try:
            return response.json()
        except ValueError:
            # Non-JSON body (HTML challenge, plain text, etc.) -- log a
            # truncated snippet to help debugging and return None.
            text_preview = (response.text or "")[:500]
            logger.exception(
                "NSEDataClient: failed to parse JSON for %s (status=%s). Response preview: %r",
                path,
                response.status_code,
                text_preview,
            )
            return None

    def get_quote(self, symbol: str) -> dict | None:
        """
        Current quote + basic company info (industry, ISIN, listing
        date) -- /api/quote-equity. Returns None rather than raising on
        a symbol NSE doesn't recognize (delisted, or a typo), same
        "missing data isn't a hard error" stance the rest of this
        codebase takes with external data.
        """
        try:
            return self._get("/api/quote-equity", params={"symbol": symbol})
        except requests.RequestException:
            logger.exception("NSEDataClient.get_quote failed for %s", symbol)
            return None

    def get_financial_results(
        self, symbol: str, period: str = "Quarterly"
    ) -> list[dict]:
        """
        period: "Quarterly" or "Annual". Returns NSE's raw list of
        reported-results rows (most recent first) -- parsing into
        FundamentalSnapshot rows is apps.investing.tasks's job, kept
        separate so this client stays a thin, honest wrapper around
        "what NSE's API actually returns" rather than mixing in this
        app's own scoring assumptions.
        """
        try:
            data = self._get(
                "/api/corporate-results",
                params={"index": "equities", "symbol": symbol, "period": period},
            )
            return data if isinstance(data, list) else (data or {}).get("data", [])
        except requests.RequestException:
            logger.exception(
                "NSEDataClient.get_financial_results failed for %s", symbol
            )
            return []

    def get_ipo_list(self) -> list[dict]:
        """
        Upcoming + currently-open IPOs. NSE's own IPO-issues endpoint
        shape -- see module docstring's caveat; this is the single
        piece of this client least likely to have a stable field
        schema, since it's the most clearly "not meant for external
        consumption" of the three calls here.
        """
        try:
            data = self._get("/api/all-upcoming-issues", params={"category": "ipo"})
            return data if isinstance(data, list) else (data or {}).get("data", [])
        except requests.RequestException:
            logger.exception("NSEDataClient.get_ipo_list failed")
            return []

    def get_historical_prices(self, symbol: str, from_date, to_date) -> list[dict]:
        """
        Daily OHLC bars for `symbol` between from_date and to_date
        (both `date` objects) -- feeds apps.investing.technical_score's
        moving-average/RSI trend read and apps.investing.valuation's
        52-week-range context. NSE's historical-data endpoint, same
        unverified-shape caveat as everything else in this client.
        """
        try:
            data = self._get(
                "/api/historical/cm/equity",
                params={
                    "symbol": symbol,
                    "series": '["EQ"]',
                    "from": from_date.strftime("%d-%m-%Y"),
                    "to": to_date.strftime("%d-%m-%Y"),
                },
            )
            return data if isinstance(data, list) else (data or {}).get("data", [])
        except requests.RequestException:
            logger.exception(
                "NSEDataClient.get_historical_prices failed for %s", symbol
            )
            return []

    def get_index_snapshot(self, index_name: str) -> dict | None:
        """
        NSE's real "equity-stockIndices" endpoint -- one call returns
        BOTH the index's own current price (as a row where
        `symbol == index_name`, e.g. "NIFTY 50") AND every current
        constituent with its own lastPrice/change/pChange, in a single
        response. This is what makes apps.investing.tasks.
        sync_index_constituents_and_prices a single API call per index
        rather than one call for membership + N calls for each
        member's price.

        index_name: NSE's own index name, e.g. "NIFTY 50", "NIFTY BANK"
        -- NOT the short F&O ticker (apps.market_data's "NIFTY"/
        "BANKNIFTY"). Returns None on failure rather than raising, same
        "missing data isn't a hard error" stance as this client's other
        methods.

        BSE indices (e.g. SENSEX) are NOT covered by this method --
        this is NSE's own API and has no BSE data. A SENSEX-equivalent
        would need a separate BSE India API client this codebase
        doesn't have; see apps/investing/tasks.py's own honest note on
        this gap rather than silently returning nothing for it.
        """
        try:
            return self._get("/api/equity-stockIndices", params={"index": index_name})
        except requests.RequestException:
            logger.exception(
                "NSEDataClient.get_index_snapshot failed for %s", index_name
            )
            return None
