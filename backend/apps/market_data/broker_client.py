"""
Thin wrapper around Angel One's SmartAPI (manual section 4: broker
integration). Kept deliberately small and isolated in this one module --
every other app touches candles/quotes through HistoricalData or the
live-price cache this module writes to, never through SmartConnect
directly. That means if the broker is ever switched (or run in
BROKER_MODE=paper, see settings), only this file changes.

Requires: smartapi-python, pyotp (both in requirements.txt).
Credentials come from .env (ANGEL_ONE_*), never hardcoded.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timedelta

import pyotp
from django.conf import settings
from django.utils import timezone as django_timezone

# A simple cross-thread, per-process rate limiter for Angel One API
# requests. Celery workers on Windows may be running with threads and
# other tasks from the same process can also hit Angel One at the same
# time, so this ensures the process does not blast the broker with
# back-to-back calls and instead spaces them out.
_SMARTAPI_RATE_LIMIT_LOCK = threading.Lock()
_SMARTAPI_LAST_REQUEST_AT = 0.0
_SMARTAPI_MIN_REQUEST_INTERVAL = 2.0  # seconds between requests across all threads
_SMARTAPI_RETRY_ATTEMPTS = 5
_SMARTAPI_RETRY_BACKOFF_FACTOR = 2.0

# SmartConnect accepts a `timeout` constructor arg and passes it straight
# through to every underlying `requests.request(..., timeout=self.timeout)`
# call (confirmed by reading the installed smartapi-python package's own
# _request() source) -- left unset, it defaults to None, which means
# "wait forever." That was a real, observed bug: a rate-limited or
# otherwise unresponsive Angel One login request (generateSession(),
# called directly in _connect() below, NOT through _smartapi_request's
# retry/backoff wrapper -- that wrapper only guards calls made AFTER
# login) hung the entire run_live_feed process indefinitely, with no
# exception ever raised for run_forever()'s own reconnect/backoff loop to
# catch -- the process just sat there needing a manual kill. Since
# `connection` is constructed ONCE per session and reused for every
# subsequent call (getCandleData/ltpData/placeOrder/getMarketData/...),
# setting this here bounds ALL of them, not just login.
_SMARTAPI_REQUEST_TIMEOUT_SECONDS = 20

# Extended-cooldown circuit breaker. AB1021 has been observed in
# practice (see this project's own incident history) as an ACCOUNT-
# LEVEL cooldown Angel One imposes for a stretch of minutes, not just
# momentary pressure -- and every one of _smartapi_request's own
# per-call retries is ANOTHER real request landing during that same
# cooldown window, which plausibly extends it rather than waiting it
# out. Once several DIFFERENT calls in a row each exhaust their own
# retries and are STILL rate-limited, this trips: every call for the
# next _SMARTAPI_COOLDOWN_SECONDS fails immediately (no request sent,
# no per-call retry wait) instead of continuing to hammer an account
# that's already in timeout. Callers (Celery tasks) already catch
# broad exceptions per symbol/timeframe and continue (see e.g.
# apps.market_data.tasks.ingest_watchlist_candles's per-combo
# try/except), so SmartApiCooldownActive just makes that existing
# per-item skip happen instantly instead of after a full 2-32s retry
# ladder -- the whole sweep clears much faster and stops adding load
# during the cooldown.
_SMARTAPI_COOLDOWN_TRIP_THRESHOLD = 2  # consecutive retry-exhausted calls before tripping
_SMARTAPI_COOLDOWN_SECONDS = 300.0  # 5 minutes
_smartapi_consecutive_rate_limit_exhaustions = 0
_smartapi_cooldown_until = 0.0


class SmartApiCooldownActive(Exception):
    """Raised instead of attempting a call while the extended-cooldown circuit breaker (above) is open."""


def _record_smartapi_rate_limit_exhaustion() -> None:
    global _smartapi_consecutive_rate_limit_exhaustions, _smartapi_cooldown_until
    with _SMARTAPI_RATE_LIMIT_LOCK:
        _smartapi_consecutive_rate_limit_exhaustions += 1
        if _smartapi_consecutive_rate_limit_exhaustions >= _SMARTAPI_COOLDOWN_TRIP_THRESHOLD:
            _smartapi_cooldown_until = time.monotonic() + _SMARTAPI_COOLDOWN_SECONDS
            logger.error(
                "Angel One: %d consecutive calls exhausted their retries still rate-limited -- "
                "tripping the circuit breaker for %.0fs (no further SmartAPI calls will even be "
                "attempted until then) instead of continuing to retry into an active cooldown.",
                _smartapi_consecutive_rate_limit_exhaustions, _SMARTAPI_COOLDOWN_SECONDS,
            )


def _reset_smartapi_rate_limit_failures() -> None:
    global _smartapi_consecutive_rate_limit_exhaustions
    if _smartapi_consecutive_rate_limit_exhaustions:
        with _SMARTAPI_RATE_LIMIT_LOCK:
            _smartapi_consecutive_rate_limit_exhaustions = 0


def _wait_for_smartapi_slot() -> None:
    global _SMARTAPI_LAST_REQUEST_AT
    with _SMARTAPI_RATE_LIMIT_LOCK:
        now = time.monotonic()
        elapsed = now - _SMARTAPI_LAST_REQUEST_AT
        if elapsed < _SMARTAPI_MIN_REQUEST_INTERVAL:
            sleep_time = (
                _SMARTAPI_MIN_REQUEST_INTERVAL - elapsed + random.uniform(0.0, 0.3)
            )
            time.sleep(sleep_time)
            now = time.monotonic()
        _SMARTAPI_LAST_REQUEST_AT = now


def _is_smartapi_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        phrase in text
        for phrase in (
            "access denied because of exceeding access rate",
            "rate limit",
            "too many requests",
            "429",
        )
    )


def _is_smartapi_rate_limit_response(response: object) -> bool:
    if not isinstance(response, dict):
        return False

    message = str(response.get("message", "") or response.get("error", "")).lower()
    return any(
        phrase in message
        for phrase in (
            "access denied because of exceeding access rate",
            "rate limit",
            "too many requests",
            "429",
        )
    )


logger = logging.getLogger(__name__)

# Angel One's historical-data API only accepts these interval strings.
# Mapped from our own HistoricalData.timeframe values so the rest of
# the codebase can keep using the shorter "5m"/"1d" convention. This
# covers every timeframe Angel One's own chart offers (source: SmartAPI
# getCandleData docs) and must stay in sync with settings.CHART_TIMEFRAMES
# and the frontend's timeframe dropdown (src/constants/market.js).
TIMEFRAME_TO_ANGEL_INTERVAL = {
    "1m": "ONE_MINUTE",
    "3m": "THREE_MINUTE",
    "5m": "FIVE_MINUTE",
    "10m": "TEN_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
    "1d": "ONE_DAY",
}

# Angel One caps how many days of history a SINGLE getCandleData call
# can span, and the cap gets tighter the finer the interval (their
# servers would otherwise have to return an enormous row count for a
# multi-year 1-minute request). Values below are Angel One's published
# per-request limits. fetch_recent_candles() uses this to clamp
# lookback_days so a request is never silently rejected -- callers who
# want more history than this should call repeatedly with adjusted
# from/to dates (a real backfill script), not raise lookback_days past
# what a single call actually supports.
MAX_LOOKBACK_DAYS = {
    "1m": 30,
    "3m": 60,
    "5m": 100,
    "10m": 100,
    "15m": 200,
    "30m": 200,
    "1h": 400,
    "1d": 2000,
}

# Angel One requires a numeric "symboltoken" per instrument, not just a
# name -- this is a small static map for the initial watchlist. A real
# deployment should replace this with a lookup against Angel One's
# published instrument master (a downloadable CSV/JSON they publish),
# refreshed periodically, rather than hardcoding tokens that can change.
#
# NIFTY/BANKNIFTY are the only two this platform actually TRADES
# (settings.WATCHLIST) -- the rest (NIFTYIT/NIFTYAUTO/NIFTYFMCG/
# NIFTYPHARMA/SENSEX) are chart/dashboard-only, added 2026-08-08 so
# apps.market_data.tasks.ingest_index_chart_candles (separate from the
# trading watchlist's own ingest_watchlist_candles) can pull real
# candle history for every index apps.investing.models.Index tracks,
# not just the two tradable ones. Symbol keys match Index.symbol
# exactly (see apps/investing/management/commands/seed_indices.py) so
# the two apps' naming can't silently drift apart. Tokens confirmed
# against Angel One's real instrument master (apps.options.
# instrument_master.find_index_token) on 2026-08-08 -- same caveat as
# NIFTY/BANKNIFTY above about tokens changing over time.
SYMBOL_TOKENS = {
    "NIFTY": {"token": "99926000", "exchange": "NSE"},
    "BANKNIFTY": {"token": "99926009", "exchange": "NSE"},
    "NIFTYIT": {"token": "99926008", "exchange": "NSE"},
    "NIFTYAUTO": {"token": "99926029", "exchange": "NSE"},
    "NIFTYFMCG": {"token": "99926021", "exchange": "NSE"},
    "NIFTYPHARMA": {"token": "99926023", "exchange": "NSE"},
    "SENSEX": {"token": "99919000", "exchange": "BSE"},
    # Confirmed live against Angel One's real instrument master
    # 2026-08-17 (apps.options.instrument_master.get_instrument_master,
    # exch_seg=NSE, instrumenttype=AMXIDX, symbol="India VIX") -- adding
    # it here is what makes it join the live SmartWebSocketV2 tick
    # subscription in broker_ws_client._connect_and_subscribe, same as
    # every other row in this dict; apps.investing.live_feed.
    # handle_index_tick then picks it up automatically once a matching
    # Index row exists (see seed_indices.DEFAULT_INDICES), no other
    # wiring needed. No SGX Nifty entry -- confirmed zero matches for
    # "SGX"/"GIFT" in the same instrument master; SGX Nifty was
    # discontinued in favor of GIFT Nifty (NSE IX, GIFT City), a
    # segment Angel One's retail API doesn't expose.
    "INDIAVIX": {"token": "99926017", "exchange": "NSE"},
}


class BrokerAuthError(Exception):
    pass


class _HardTimeout(Exception):
    """Raised by _call_with_hard_timeout when the wrapped call doesn't finish in time."""


def _call_with_hard_timeout(func, timeout_seconds: float, *args, **kwargs):
    """
    Runs `func` on a daemon thread and raises _HardTimeout if it hasn't
    finished within `timeout_seconds` -- a second, independent bound on
    top of SmartConnect's own `timeout=` constructor arg (see
    _SMARTAPI_REQUEST_TIMEOUT_SECONDS above), for the case that arg
    doesn't actually end up bounding the call in practice. Observed for
    real in this environment: generateSession() (called directly by
    _connect() below, not through _smartapi_request's retry wrapper --
    that wrapper only guards calls made AFTER login) hung for several
    minutes with SmartConnect's own timeout=20 already set and no
    exception ever raised, which strongly suggests requests/urllib3's
    timeout wasn't actually bounding whatever stalled (a DNS-resolution
    hang is a known gap in some requests/urllib3 versions -- their
    `timeout` covers the connect/read phases, not always the DNS lookup
    that happens before either). Whatever the exact underlying cause,
    the actual problem this exists to prevent is worse than any single
    root cause: run_live_feed's whole reconnect/backoff loop (see
    LiveFeedClient.run_forever) depends on _connect() eventually
    RAISING on failure so it can log, back off, and retry -- a call that
    just hangs forever instead defeats that self-healing entirely, with
    no way to recover short of an external process kill. If `func`
    really is permanently stuck (blocked in a syscall Python can't
    interrupt), this leaks one idle background thread rather than
    killing the whole process -- an acceptable trade next to "the
    process can never recover on its own."
    """
    result: dict = {}

    def runner():
        try:
            result["value"] = func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the calling thread below, must not be lost
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise _HardTimeout(f"{getattr(func, '__qualname__', func)} did not complete within {timeout_seconds}s.")
    if "error" in result:
        raise result["error"]
    return result["value"]


_shared_client_lock = threading.Lock()
_shared_client: "BrokerClient | None" = None


def get_broker_client() -> "BrokerClient":
    """
    Returns ONE BrokerClient shared for the lifetime of this process.

    BrokerClient's own docstring below has always said "one instance per
    process, not per-request" -- but every recurring caller
    (apps.market_data.tasks.ingest_watchlist_candles/
    ingest_index_chart_candles, apps.investing.tasks.
    _sync_index_prices_via_angelone, apps.execution.live_executor) was
    instead doing `client = BrokerClient()` fresh on every single
    invocation, which starts `_smart_connect=None` again every time --
    meaning a brand new, UNTHROTTLED Angel One login
    (`generateSession()`, which _connect() calls directly, not through
    _smartapi_request's rate-limit/retry wrapper -- that wrapper only
    guards calls made AFTER login) on every task run. With
    ingest_watchlist_candles alone firing every 60 seconds, that's a
    fresh login a minute, on top of the 5-minute sweep, the 10-minute
    index-chart sweep, and investing's 3-minute index-price sync -- all
    in the same worker process, all burning through Angel One's login
    rate budget independently of each other and of the 2-second
    inter-request spacing _smartapi_request already enforces for
    everything after login. This is what was actually surfacing as
    "Access denied because of exceeding access rate" on the REST calls
    that came after -- not those calls themselves. Every recurring
    entry point should call this function instead of `BrokerClient()`
    directly; a one-shot management command (e.g.
    backfill_historical_candles) instantiating its own is fine, since
    the command's own process only ever calls it once anyway.
    """
    global _shared_client
    with _shared_client_lock:
        if _shared_client is None:
            _shared_client = BrokerClient()
        return _shared_client


class BrokerClient:
    """
    One instance per process (Celery worker), not per-request -- Angel
    One's session/JWT is meant to be reused across calls, not
    re-authenticated on every candle fetch. `_connect()` is lazy so
    importing this module never triggers a live login attempt (e.g.
    during Django's app-loading or in tests where BROKER_MODE=paper and
    this should never be touched at all). Recurring callers should
    fetch the process-shared instance via get_broker_client() above
    rather than instantiating this directly -- see that function's
    docstring for why that distinction matters in practice.
    """

    def __init__(self):
        self._smart_connect = None
        self._jwt_token = None
        self._feed_token = None

    def _smartapi_request(self, func, *args, **kwargs):
        with _SMARTAPI_RATE_LIMIT_LOCK:
            remaining_cooldown = _smartapi_cooldown_until - time.monotonic()
        if remaining_cooldown > 0:
            raise SmartApiCooldownActive(
                f"Angel One extended-cooldown circuit breaker is open "
                f"({remaining_cooldown:.0f}s remaining) -- not attempting this call. "
                f"See apps.market_data.broker_client's circuit-breaker comment for why."
            )

        attempt = 0
        while True:
            attempt += 1
            _wait_for_smartapi_slot()
            try:
                response = func(*args, **kwargs)
            except Exception as exc:
                if (
                    attempt < _SMARTAPI_RETRY_ATTEMPTS
                    and _is_smartapi_rate_limit_error(exc)
                ):
                    backoff = _SMARTAPI_RETRY_BACKOFF_FACTOR * (2 ** (attempt - 1))
                    logger.warning(
                        "Angel One rate limit hit on attempt %d/%d, backing off %.1fs: %s",
                        attempt,
                        _SMARTAPI_RETRY_ATTEMPTS,
                        backoff,
                        exc,
                    )
                    time.sleep(backoff)
                    continue
                if _is_smartapi_rate_limit_error(exc):
                    _record_smartapi_rate_limit_exhaustion()
                raise

            if attempt < _SMARTAPI_RETRY_ATTEMPTS and _is_smartapi_rate_limit_response(
                response
            ):
                backoff = _SMARTAPI_RETRY_BACKOFF_FACTOR * (2 ** (attempt - 1))
                backoff += random.uniform(0.0, 0.5)
                logger.warning(
                    "Angel One rate limit response on attempt %d/%d, backing off %.1fs: %s",
                    attempt,
                    _SMARTAPI_RETRY_ATTEMPTS,
                    backoff,
                    response,
                )
                time.sleep(backoff)
                continue

            if _is_smartapi_rate_limit_response(response):
                _record_smartapi_rate_limit_exhaustion()
            else:
                _reset_smartapi_rate_limit_failures()
            return response

    def _connect(self):
        if self._smart_connect is not None:
            return self._smart_connect

        # Imported lazily (not at module top) so `smartapi-python` isn't
        # a hard import-time dependency for anything running in paper
        # mode -- only actually needed once a live connection is made.
        from SmartApi import SmartConnect

        api_key = settings.ANGEL_ONE_API_KEY
        client_id = settings.ANGEL_ONE_CLIENT_ID
        password = settings.ANGEL_ONE_PASSWORD
        totp_secret = settings.ANGEL_ONE_TOTP_SECRET

        if not all([api_key, client_id, password, totp_secret]):
            raise BrokerAuthError(
                "Angel One credentials are missing from settings/.env -- "
                "set ANGEL_ONE_API_KEY/CLIENT_ID/PASSWORD/TOTP_SECRET, or "
                "run in BROKER_MODE=paper instead."
            )

        totp = pyotp.TOTP(totp_secret).now()
        # SmartConnect(...) construction itself is wrapped too, not just
        # generateSession() -- observed hanging BEFORE any HTTP request is
        # even made, most likely inside ssl.create_default_context()
        # (SmartConnect.__init__ builds one unconditionally): on Windows
        # this can trigger a certificate-store/revocation-check network
        # call the first time it runs in a process, which isn't bounded
        # by requests' own `timeout=` at all since no request has been
        # sent yet. See _call_with_hard_timeout's own docstring for the
        # full reasoning on why a call hanging here must still surface as
        # an exception, not an unrecoverable process hang.
        connection = _call_with_hard_timeout(
            SmartConnect, _SMARTAPI_REQUEST_TIMEOUT_SECONDS,
            api_key=api_key, timeout=_SMARTAPI_REQUEST_TIMEOUT_SECONDS,
        )
        session = _call_with_hard_timeout(
            connection.generateSession, _SMARTAPI_REQUEST_TIMEOUT_SECONDS, client_id, password, totp,
        )

        if not session.get("status"):
            raise BrokerAuthError(f"Angel One login failed: {session.get('message')}")

        # jwtToken/feedToken are only used by the SmartWebSocketV2 live-tick
        # connection (apps/market_data/broker_ws_client.py) -- SmartConnect's
        # own REST methods (getCandleData/ltpData/placeOrder/...) don't need
        # them, which is why nothing before this read them. Captured here
        # rather than re-deriving from a second generateSession call, since
        # Angel One invalidates the previous session's tokens on a fresh
        # login for the same client.
        session_data = session.get("data") or {}
        self._jwt_token = session_data.get("jwtToken")
        self._feed_token = session_data.get("feedToken")

        self._smart_connect = connection
        logger.info("Angel One session established for client %s", client_id)
        return connection

    def get_feed_credentials(self) -> dict:
        """
        Returns what apps.market_data.broker_ws_client.LiveFeedClient needs
        to open a SmartWebSocketV2 connection -- a genuinely different
        SmartAPI surface than the SmartConnect REST client above, but backed
        by the same login (_connect() is idempotent/lazy, see this class's
        own docstring).
        """
        self._connect()
        return {
            "api_key": settings.ANGEL_ONE_API_KEY,
            "client_code": settings.ANGEL_ONE_CLIENT_ID,
            "jwt_token": self._jwt_token,
            "feed_token": self._feed_token,
        }

    def _fetch_candle_rows(
        self, exchange: str, symboltoken: str, timeframe: str, lookback_days: int, to_date: datetime | None,
    ) -> list[dict]:
        """
        The actual Angel One getCandleData call, generic over ANY
        exchange+symboltoken -- shared by fetch_recent_candles (below,
        restricted to the SYMBOL_TOKENS watchlist) and
        fetch_candles_for_token (any instrument, e.g. an NFO option
        contract's own symbol_token) so there is exactly one place that
        builds the request/parses the response, not two that could
        drift. Returns dicts with just {timestamp, open, high, low,
        close, volume} -- callers attach their own symbol/timeframe/
        source framing on top.
        """
        if timeframe not in TIMEFRAME_TO_ANGEL_INTERVAL:
            raise ValueError(
                f"Unsupported timeframe {timeframe!r} for Angel One ingestion."
            )

        # Clamp rather than error: the recurring ingestion task calls
        # this with the same small lookback_days for every timeframe
        # (it doesn't know per-interval limits), so silently respecting
        # Angel One's actual per-interval cap keeps that call working
        # instead of throwing on the finer intervals.
        max_days = MAX_LOOKBACK_DAYS.get(timeframe, lookback_days)
        if lookback_days > max_days:
            logger.warning(
                "lookback_days=%d exceeds Angel One's %s limit of %d days for a single "
                "request -- clamping. Call repeatedly with adjusted date ranges for more history.",
                lookback_days,
                timeframe,
                max_days,
            )
            lookback_days = max_days

        connection = self._connect()
        # django_timezone.localtime() converts to settings.TIME_ZONE
        # (Asia/Kolkata) regardless of the host machine/container's own
        # system clock timezone -- plain datetime.now() would silently
        # use whatever the OS is set to, which is wrong the moment this
        # ever runs somewhere that isn't already set to IST (a fresh
        # cloud VM defaults to UTC, for example) and Angel One's
        # fromdate/todate strings need to be in IST to mean what NSE's
        # own trading-hours convention means. Getting this wrong doesn't
        # raise an error -- it silently fetches the wrong 5.5-hour
        # window, which is a worse failure mode than a crash.
        to_date = django_timezone.localtime(to_date) if to_date is not None else django_timezone.localtime(django_timezone.now())
        from_date = to_date - timedelta(days=lookback_days)

        response = self._smartapi_request(
            connection.getCandleData,
            {
                "exchange": exchange,
                "symboltoken": symboltoken,
                "interval": TIMEFRAME_TO_ANGEL_INTERVAL[timeframe],
                "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
                "todate": to_date.strftime("%Y-%m-%d %H:%M"),
            },
        )

        if not response.get("status"):
            raise RuntimeError(
                f"Angel One candle fetch failed for token={symboltoken}: {response.get('message')}"
            )

        # Angel One returns rows as [timestamp, open, high, low, close, volume]
        rows = []
        for row in response.get("data", []):
            ts, o, h, l, c, v = row
            timestamp = datetime.fromisoformat(ts)
            if django_timezone.is_naive(timestamp):
                timestamp = django_timezone.make_aware(
                    timestamp,
                    django_timezone.get_default_timezone(),
                )
            rows.append({"timestamp": timestamp, "open": o, "high": h, "low": l, "close": c, "volume": v})
        return rows

    def fetch_recent_candles(
        self, symbol: str, timeframe: str, lookback_days: int = 5, to_date: datetime | None = None,
    ) -> list[dict]:
        """
        Returns a list of dicts shaped exactly like HistoricalData's
        fields (minus id/source) so apps.market_data.tasks can bulk-
        create them directly. lookback_days is intentionally small
        (default 5) for the *recurring* ingestion job -- pulling years
        of history is apps.market_data.management.commands.
        backfill_historical_candles's job instead; the recurring job
        just needs to catch up on whatever's new since the last run,
        and unique_together on HistoricalData makes re-fetching
        overlapping days harmless.

        to_date: defaults to now (every existing caller's behavior,
        unchanged) -- backfill_historical_candles passes an explicit,
        walked-backward value instead, since a single Angel One request
        is capped per-interval (MAX_LOOKBACK_DAYS below, e.g. 30 days
        for 1m candles) well short of a year; getting a full year of
        finer-than-daily history means calling this repeatedly with
        `to_date` stepped back by one window each time, not one call
        with a bigger lookback_days (which just gets silently clamped,
        see below).
        """
        if symbol not in SYMBOL_TOKENS:
            raise ValueError(
                f"No symboltoken configured for {symbol} -- add it to "
                f"SYMBOL_TOKENS in apps/market_data/broker_client.py."
            )
        token_info = SYMBOL_TOKENS[symbol]
        rows = self._fetch_candle_rows(token_info["exchange"], token_info["token"], timeframe, lookback_days, to_date)
        return [
            {"symbol": symbol, "timeframe": timeframe, "source": "angel_one", **row}
            for row in rows
        ]

    def fetch_candles_for_token(
        self, exchange: str, symboltoken: str, timeframe: str, lookback_days: int = 5, to_date: datetime | None = None,
    ) -> list[dict]:
        """
        Same Angel One getCandleData call as fetch_recent_candles, but
        for ANY instrument's own exchange+symboltoken -- not restricted
        to the small SYMBOL_TOKENS watchlist. Used by
        apps.options.candle_service for individual NFO option contracts
        (each OptionContract already carries its own real symbol_token
        from the instrument master, see apps.options.contract_sync),
        which will never appear in SYMBOL_TOKENS (that dict is indices
        only). Returns bare {timestamp, open, high, low, close, volume}
        dicts -- callers attach their own contract/timeframe framing.
        """
        return self._fetch_candle_rows(exchange, symboltoken, timeframe, lookback_days, to_date)

    def check_feed_health(self) -> dict:
        """
        A cheap liveness probe: fetches a tiny amount of the most liquid
        symbol's recent data and measures how long it takes. Used by
        apps.monitoring (via apps/market_data/tasks.py) to populate
        FeedHealthCheck, which apps.risk.engine's pre-trade checks
        consult before approving any trade (manual section 16:
        "Validate feed freshness").
        """
        started = django_timezone.localtime(django_timezone.now())
        try:
            self.get_ltp("NIFTY")
            latency_ms = int(
                (
                    django_timezone.localtime(django_timezone.now()) - started
                ).total_seconds()
                * 1000
            )
            return {"is_healthy": True, "latency_ms": latency_ms, "detail": ""}
        except Exception as exc:
            logger.exception("Feed health check failed")
            return {"is_healthy": False, "latency_ms": None, "detail": str(exc)}

    def get_ltp(self, symbol: str) -> float:
        """
        Last traded price -- used by the live executor to mark
        positions to market and to sanity-check a fill against the
        price it expected before placing a stop/target modification.
        """
        if symbol not in SYMBOL_TOKENS:
            raise ValueError(f"No symboltoken configured for {symbol}.")
        token_info = SYMBOL_TOKENS[symbol]
        connection = self._connect()
        response = self._smartapi_request(
            connection.ltpData,
            token_info["exchange"],
            symbol,
            token_info["token"],
        )
        if not response.get("status"):
            raise RuntimeError(
                f"LTP fetch failed for {symbol}: {response.get('message')}"
            )
        return float(response["data"]["ltp"])

    def get_quote_by_token(self, exchange: str, tradingsymbol: str, token: str) -> dict:
        """
        Generic LTP quote fetch for ANY instrument Angel One has a token
        for -- unlike get_ltp() above, not restricted to SYMBOL_TOKENS
        (the 2-symbol options watchlist). Added for apps.investing's
        index-price sync (apps/options/instrument_master.find_index_token
        resolves the token for an arbitrary index by name), keeping this
        module's own "every SmartConnect call lives in one file" rule
        intact rather than having apps.investing import SmartConnect
        directly.

        Returns {"ltp", "close", "open", "high", "low"} -- "close" is
        Angel One's own field name for PREVIOUS close (a common
        quote-API naming convention, confirmed against a real response),
        not the current bar's close; callers computing day-change should
        use ltp - close, not treat "close" as today's close.
        """
        connection = self._connect()
        response = self._smartapi_request(
            connection.ltpData,
            exchange,
            tradingsymbol,
            token,
        )
        if not response.get("status"):
            raise RuntimeError(
                f"Quote fetch failed for {tradingsymbol}: {response.get('message')}"
            )
        data = response["data"]
        return {
            "ltp": float(data["ltp"]),
            "close": float(data["close"]),
            "open": float(data["open"]),
            "high": float(data["high"]),
            "low": float(data["low"]),
        }

    def place_order(
        self,
        symbol: str,
        transaction_type: str,
        qty: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        symbol_token: str | None = None,
        exchange: str | None = None,
        tradingsymbol: str | None = None,
    ) -> str:
        """
        Places a real order. transaction_type is "BUY" or "SELL".
        Returns Angel One's order_id.

        DELIBERATELY THIN: this function does NOT re-check any risk
        limits, does NOT decide position size, and does NOT retry on
        failure. All of that must have already happened upstream (in
        apps.risk.engine + apps.execution.live_executor) before this is
        ever called -- by the time code reaches here, the decision to
        trade has already been made and approved. This function's only
        job is "send exactly this order to the broker, and report
        exactly what came back."

        symbol_token/exchange/tradingsymbol: optional overrides so this
        same function can place an order for ANY resolved instrument --
        e.g. apps.execution.live_executor placing a real NFO option
        order using an apps.options.OptionContract's own symbol_token/
        tradingsymbol -- not just the small hard-coded SYMBOL_TOKENS map
        below (index cash-segment symbols only). When all three are
        given, the SYMBOL_TOKENS lookup is skipped entirely; `symbol`
        is still used as `tradingsymbol` unless `tradingsymbol` is also
        explicitly given. Every pre-existing caller passes none of
        these and is unaffected.
        """
        if symbol_token and exchange:
            token = symbol_token
            exch = exchange
            trading_symbol = tradingsymbol or symbol
        else:
            if symbol not in SYMBOL_TOKENS:
                raise ValueError(f"No symboltoken configured for {symbol}.")
            token_info = SYMBOL_TOKENS[symbol]
            token = token_info["token"]
            exch = token_info["exchange"]
            trading_symbol = symbol
        connection = self._connect()

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": trading_symbol,
            "symboltoken": token,
            "transactiontype": transaction_type,
            "exchange": exch,
            "ordertype": order_type,
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(price) if order_type == "LIMIT" else "0",
            "quantity": str(qty),
        }
        response = self._smartapi_request(connection.placeOrder, order_params)

        # smartapi-python's placeOrder return shape has varied across
        # versions (sometimes the order id directly, sometimes a dict) --
        # handle both rather than assuming one.
        if isinstance(response, dict):
            if not response.get("status", True):
                raise RuntimeError(
                    f"Order placement failed for {symbol}: {response.get('message')}"
                )
            order_id = response.get("data", {}).get("orderid") or response.get(
                "orderid"
            )
        else:
            order_id = response

        if not order_id:
            raise RuntimeError(
                f"Order placement for {symbol} returned no order id: {response!r}"
            )

        logger.info(
            "Placed %s order for %s x%d -> order_id=%s",
            transaction_type,
            symbol,
            qty,
            order_id,
        )
        return str(order_id)

    def get_order_status(self, order_id: str) -> dict:
        """
        Returns Angel One's order-book entry for this order_id (status,
        filled quantity, average fill price). apps.execution.live_executor
        polls this after placing an order rather than assuming an
        instant, complete fill -- intraday market orders on index
        derivatives usually do fill immediately, but "usually" is not a
        safe assumption for code that risks real money.
        """
        connection = self._connect()
        order_book = self._smartapi_request(connection.orderBook)
        if not order_book.get("status"):
            raise RuntimeError(
                f"Could not fetch order book: {order_book.get('message')}"
            )

        for order in order_book.get("data", []) or []:
            if str(order.get("orderid")) == str(order_id):
                return order

        raise RuntimeError(f"Order {order_id} not found in order book.")

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        """
        Attempts to cancel a pending order -- used by
        apps.execution.live_executor._wait_for_fill when our own poll
        window times out (order still pending/open after
        FILL_POLL_MAX_ATTEMPTS), to reduce the window in which a market
        order could still fill at the broker AFTER this codebase has
        already given up waiting and marked the signal REJECTED --
        an order that fills after we've stopped tracking it would be a
        real position with zero risk-management/kill-switch visibility.

        This REDUCES that risk, it doesn't eliminate it: a cancel
        request can itself fail if the order fills in the brief window
        between our last status poll and the cancel request landing
        (very unlikely for a liquid index derivative, but not
        impossible). Returns True if Angel One confirms the cancel,
        False otherwise -- callers must still treat "cancel failed" as
        "this order may still be live at the broker" and log/alert
        accordingly rather than assuming it's safely gone.
        """
        connection = self._connect()
        try:
            response = self._smartapi_request(connection.cancelOrder, order_id, variety)
        except Exception:
            logger.exception("cancel_order request failed for order_id=%s", order_id)
            return False

        if isinstance(response, dict):
            return bool(response.get("status", False))
        return bool(response)
