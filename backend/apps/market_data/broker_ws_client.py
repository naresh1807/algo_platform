"""
The actual Angel One SmartWebSocketV2 live-tick connection --
apps/investing/live_feed.py (before this change) documented this as
never built anywhere in this codebase; this module is that build.

Genuinely a different SmartAPI surface than
apps.market_data.broker_client.BrokerClient's SmartConnect REST calls
(getCandleData/ltpData/placeOrder/...): this opens one persistent
WebSocket, authenticated with the feedToken from BrokerClient's own
login (get_feed_credentials()), and receives a continuous tick stream
instead of polling.

HONESTY NOTE, same spirit as every other broker integration in this
codebase (apps.market_data.broker_client, apps.options.broker_client):
the field names and paise-vs-rupee scaling below are implemented per
Angel One's documented SmartWebSocketV2 QUOTE-mode payload shape, but
this has not been run against a live session from here (no network
access, no funded account in this environment). on_data logs the
FIRST raw payload it ever receives at INFO specifically so this is a
30-second visual check the first time this runs for real, not a
silent assumption -- if field names differ, this is the only function
that needs to change.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from django.conf import settings

from .broker_client import BrokerClient
from .market_hours import is_market_open
from .tick_aggregator import CandleAggregator

logger = logging.getLogger(__name__)

# Angel One's WebSocket feed sends price fields as integers in paise
# (rupees * 100) -- documented SmartWebSocketV2 convention, distinct
# from SmartConnect's REST responses (getCandleData/ltpData), which
# already return rupees. See this module's own honesty note above.
PAISE_TO_RUPEE = 100

# SmartWebSocketV2's subscribe() exchangeType codes (Angel One's own
# published mapping, not something this codebase invents). NFO (nse_fo)
# added alongside NSE/BSE cash once option ticks needed a token list of
# their own -- indices never needed it since NIFTY/BANKNIFTY's own
# tokens live on NSE/BSE cash, not NFO.
EXCHANGE_TYPE = {"NSE": 1, "BSE": 3, "NFO": 2}

# Subscription mode: 2 = QUOTE -- LTP + day OHLC + cumulative day
# volume + previous close in one payload. Chosen over mode 1 (LTP-only)
# specifically because CandleAggregator needs day_volume (for per-bucket
# volume deltas) and apps.investing.live_feed.handle_index_tick needs
# closed_price (previous close, for change/change_pct) -- both come
# free in QUOTE mode from a single subscription, no second call needed.
SUBSCRIBE_MODE_QUOTE = 2

# Mode 3 = SNAP_QUOTE -- everything QUOTE gives PLUS open_interest and
# best_5_buy_data/best_5_sell_data (top-of-book price at index [0]),
# confirmed by directly reading the installed smartapi-python package's
# own _parse_binary_data source (not guessed from docs alone). Options
# need this (apps.options.live_feed.handle_option_tick needs OI +
# bid/ask); indices stay on QUOTE since neither OI nor a bid/ask ladder
# means anything for an index value itself.
SUBSCRIBE_MODE_SNAP_QUOTE = 3

_RECONNECT_BACKOFF_INITIAL = 5
_RECONNECT_BACKOFF_MAX = 60
_MARKET_CLOSED_POLL_INTERVAL = 60

# SmartWebSocketV2.connect() calls websocket-client's WebSocketApp.
# run_forever() with no ping_timeout/connect-timeout of its own -- if
# the initial WS handshake itself never completes (observed for real:
# on_open never fired, no error, no exception, run_forever() just never
# returned), this codebase's own reconnect/backoff loop (run_forever()
# below) never gets a chance to run either, since it's all one blocking
# call. This bounds how long a single connection ATTEMPT is allowed to
# stay un-opened before being forced closed and treated as a failure --
# same "a hang must still surface as an exception" reasoning as
# apps.market_data.broker_client._call_with_hard_timeout, applied here
# via SmartWebSocketV2's own close_connection() (confirmed present on
# the installed package) since a blocking run_forever() can't be
# time-bounded any other way without a second thread to call it from.
_CONNECT_TIMEOUT_SECONDS = 20


class LiveFeedClient:
    """
    One instance per process (the run_live_feed management command).
    `on_index_tick`, if given, is called for every tick regardless of
    symbol (apps.investing.live_feed.handle_index_tick uses this to
    drive the dashboard's index ticker cards); candle aggregation only
    runs for settings.WATCHLIST symbols, matching
    apps.market_data.tasks.ingest_watchlist_candles's own scope.

    Index and option ticks are processed on two SEPARATE background
    worker threads, each fed by its own queue, rather than directly in
    smartapi-python's `on_data` callback (which runs on the single
    thread that owns the actual WebSocket connection). This matters:
    apps.options.live_feed.handle_option_tick does real, non-trivial
    work per tick (a DB SELECT for the previous OI, a Newton-Raphson IV
    solve, a DB INSERT) -- fine for one contract, but with 100+ option
    contracts subscribed this can add up to real wall-clock time. If
    that work ran directly in `on_data`, a burst of option ticks would
    back up the ONE thread reading off the actual socket, delaying
    delivery of everything queued behind it -- including index ticks,
    which need to stay cheap and current for the dashboard's "exact
    price" chart/ticker to mean anything. Splitting them means a slow
    option-processing backlog can never make the index price lag.
    """

    # After this many consecutive failed connection attempts, assume the
    # cached session itself (not just the socket) has gone bad -- e.g.
    # Angel One's daily session validity window lapsing on a long-running
    # process -- and force a fresh login on the next attempt. Kept high
    # enough that ordinary network blips (which don't need a new login,
    # see _connect_and_subscribe's own note) never trigger it.
    _FORCE_RELOGIN_AFTER_FAILURES = 3

    def __init__(
        self,
        aggregator: CandleAggregator,
        on_index_tick=None,
        option_tokens_provider=None,
        on_option_tick=None,
    ):
        self._aggregator = aggregator
        self._on_index_tick = on_index_tick
        # A callable (not a static dict) so run_forever() can re-fetch a
        # fresh symbol_token -> OptionContract.id mapping on every
        # (re)connect -- reconnects already happen on their own cadence
        # (startup, any backoff retry), so this is what keeps the
        # option subscription current after sync_watchlist_option_
        # contracts' weekly nearest-expiry rollover, without a separate
        # timer thread. Kept out of this module's own responsibility
        # (no Django model imports here -- see the module docstring:
        # this file is deliberately just the transport layer) by taking
        # the provider as an injected callable, same spirit as
        # on_index_tick/on_option_tick below.
        self._option_tokens_provider = option_tokens_provider
        self._option_tokens: dict[str, int] = {}
        self._on_option_tick = on_option_tick
        self._logged_first_tick = False
        # ONE BrokerClient for the process lifetime, reused across
        # reconnects -- BrokerClient._connect() is already lazy/cached
        # per-instance, so this means a reconnect after a transient
        # network blip reuses the existing session/feed_token instead of
        # calling Angel One's login endpoint again. Creating a fresh
        # BrokerClient() per reconnect attempt (the original version of
        # this method) logs in on every single retry, which is very
        # likely what was tipping the Celery worker's own REST calls
        # into "Access denied because of exceeding access rate" --
        # logins aren't rate-limited/retried the way REST calls are
        # (see BrokerClient._connect()'s own docstring).
        self._broker_client = BrokerClient()
        self._consecutive_failures = 0

        # See the class docstring for why these are split. Bounded so a
        # sustained processing backlog shows up as dropped-and-logged
        # ticks (a visible symptom to fix) rather than unbounded memory
        # growth -- the option queue is sized larger since it fans out
        # over far more instruments than the ~7-symbol index queue ever
        # will. token_to_symbol lives here (not as a local in
        # _connect_and_subscribe) so the worker threads -- which run for
        # the LiveFeedClient's whole lifetime, started once below, not
        # per-reconnect -- always see whatever the most recent connect
        # populated, without needing to be restarted on every reconnect.
        self._index_queue: queue.Queue = queue.Queue(maxsize=1000)
        self._option_queue: queue.Queue = queue.Queue(maxsize=4000)
        self._token_to_symbol: dict[str, str] = {}
        threading.Thread(target=self._index_worker_loop, daemon=True, name="live-feed-index").start()
        threading.Thread(target=self._option_worker_loop, daemon=True, name="live-feed-options").start()

    def run_forever(self) -> None:
        """
        Blocking. Reconnects with exponential backoff on any error or
        clean close, forever, until the process is killed -- this is
        the resilience layer on top of SmartWebSocketV2's own internal
        retry (which gives up after a fixed attempt count), covering
        things like a full session/day rollover or a network outage
        longer than that library-level retry budget.
        """
        backoff = _RECONNECT_BACKOFF_INITIAL
        while True:
            is_open, reason = is_market_open()
            if not is_open:
                logger.info("run_live_feed: market closed (%s) -- checking again in %ds.", reason, _MARKET_CLOSED_POLL_INTERVAL)
                time.sleep(_MARKET_CLOSED_POLL_INTERVAL)
                continue

            if self._consecutive_failures >= self._FORCE_RELOGIN_AFTER_FAILURES:
                logger.warning(
                    "run_live_feed: %d consecutive failures -- forcing a fresh Angel One login before retrying.",
                    self._consecutive_failures,
                )
                self._broker_client = BrokerClient()
                self._consecutive_failures = 0

            if self._option_tokens_provider is not None:
                try:
                    self._option_tokens = self._option_tokens_provider() or {}
                except Exception:
                    logger.exception("run_live_feed: option_tokens_provider failed -- reconnecting without an option subscription this attempt.")
                    self._option_tokens = {}

            try:
                self._connect_and_subscribe()
                backoff = _RECONNECT_BACKOFF_INITIAL  # reset after any clean session
                self._consecutive_failures = 0
            except Exception:
                logger.exception("Live feed connection dropped -- reconnecting in %ds.", backoff)
                self._consecutive_failures += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

    def _connect_and_subscribe(self) -> None:
        # Imported lazily, same reasoning as BrokerClient._connect()'s own
        # lazy SmartConnect import -- smartapi-python shouldn't be a
        # hard import-time dependency for anything that never runs this.
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        from .broker_client import SYMBOL_TOKENS

        creds = self._broker_client.get_feed_credentials()
        sws = SmartWebSocketV2(
            creds["jwt_token"], creds["api_key"], creds["client_code"], creds["feed_token"],
        )

        token_list = _build_token_list(SYMBOL_TOKENS)
        self._token_to_symbol = {info["token"]: symbol for symbol, info in SYMBOL_TOKENS.items()}
        option_token_list = _build_nfo_token_list(self._option_tokens)

        opened = threading.Event()

        def on_open(wsapp):
            opened.set()
            logger.info("Angel One live feed connected -- subscribing to %d symbols.", len(SYMBOL_TOKENS))
            sws.subscribe("live_feed", SUBSCRIBE_MODE_QUOTE, token_list)
            if option_token_list:
                logger.info(
                    "Angel One live feed: subscribing to %d option contracts.", len(self._option_tokens),
                )
                sws.subscribe("live_feed_options", SUBSCRIBE_MODE_SNAP_QUOTE, option_token_list)

        def on_data(wsapp, message):
            # Deliberately just routing + a non-blocking enqueue here --
            # see the class docstring for why no actual tick processing
            # (DB writes, IV solving) happens on this thread.
            if not self._logged_first_tick:
                self._logged_first_tick = True
                logger.info("run_live_feed: first raw tick payload (verify field names/units against this): %r", message)

            token = message.get("token")
            target_queue = self._option_queue if token in self._option_tokens else self._index_queue
            try:
                target_queue.put_nowait(message)
            except queue.Full:
                logger.warning(
                    "run_live_feed: %s tick queue full -- dropping a tick for token=%s.",
                    "option" if target_queue is self._option_queue else "index", token,
                )

        def on_error(category, message):
            # smartapi-python's SmartWebSocketV2 calls this as
            # on_error("Reconnect Error"/"Max retry attempt reached", detail)
            # -- two plain strings, NOT (wsapp, error) despite what that
            # naming pattern might suggest elsewhere in this library.
            # Confirmed against the installed package's own call sites.
            logger.error("Angel One live feed error [%s]: %s", category, message)

        def on_close(wsapp):
            logger.warning("Angel One live feed connection closed.")

        sws.on_open = on_open
        sws.on_data = on_data
        sws.on_error = on_error
        sws.on_close = on_close

        def _watchdog():
            if not opened.is_set():
                logger.warning(
                    "Angel One live feed: WebSocket handshake did not open within %ds -- forcing it closed "
                    "so run_forever()'s own reconnect/backoff can retry instead of hanging indefinitely.",
                    _CONNECT_TIMEOUT_SECONDS,
                )
                sws.close_connection()

        watchdog_timer = threading.Timer(_CONNECT_TIMEOUT_SECONDS, _watchdog)
        watchdog_timer.daemon = True
        watchdog_timer.start()
        try:
            sws.connect()  # blocking for the lifetime of this connection
        finally:
            watchdog_timer.cancel()

        if not opened.is_set():
            raise TimeoutError(
                f"Angel One live feed: WebSocket connection never opened within {_CONNECT_TIMEOUT_SECONDS}s."
            )

    def _index_worker_loop(self) -> None:
        while True:
            message = self._index_queue.get()
            try:
                self._process_index_tick(message)
            except Exception:
                logger.exception("Index tick processing failed for message=%r", message)

    def _option_worker_loop(self) -> None:
        while True:
            message = self._option_queue.get()
            try:
                contract_id = self._option_tokens.get(message.get("token"))
                if contract_id is not None:
                    self._process_option_tick(contract_id, message)
            except Exception:
                logger.exception("Option tick processing failed for message=%r", message)

    def _process_index_tick(self, message: dict) -> None:
        token = message.get("token")
        symbol = self._token_to_symbol.get(token)
        if symbol is None:
            return

        raw_ltp = message.get("last_traded_price")
        if raw_ltp is None:
            return
        ltp = raw_ltp / PAISE_TO_RUPEE

        day_volume = message.get("volume_trade_for_the_day") or 0

        raw_close = message.get("closed_price")
        close_price = (raw_close / PAISE_TO_RUPEE) if raw_close else None

        if symbol in settings.WATCHLIST:
            try:
                self._aggregator.on_tick(symbol, ltp, day_volume)
            except Exception:
                logger.exception("CandleAggregator.on_tick failed for %s", symbol)

        if self._on_index_tick is not None:
            try:
                self._on_index_tick(symbol, ltp, close_price)
            except Exception:
                logger.exception("on_index_tick failed for %s", symbol)

    def _process_option_tick(self, contract_id: int, message: dict) -> None:
        if self._on_option_tick is None:
            return

        raw_ltp = message.get("last_traded_price")
        if raw_ltp is None:
            return
        ltp = raw_ltp / PAISE_TO_RUPEE

        oi = message.get("open_interest") or 0
        volume = message.get("volume_trade_for_the_day") or 0

        # Top-of-book only (index 0) -- these lists are already the
        # library's OWN best_5_buy_data/best_5_sell_data output (its
        # _parse_binary_data swaps an internal buy/sell pair before
        # returning, confirmed by reading the installed source; what
        # arrives here under these two keys is already correct from the
        # caller's side, nothing to un-swap). Price is paise, same as
        # last_traded_price.
        buy_levels = message.get("best_5_buy_data") or []
        sell_levels = message.get("best_5_sell_data") or []
        bid = (buy_levels[0]["price"] / PAISE_TO_RUPEE) if buy_levels else None
        ask = (sell_levels[0]["price"] / PAISE_TO_RUPEE) if sell_levels else None

        try:
            self._on_option_tick(contract_id, ltp, oi, volume, bid, ask)
        except Exception:
            logger.exception("on_option_tick failed for contract_id=%s", contract_id)


def _build_token_list(symbol_tokens: dict) -> list[dict]:
    by_exchange: dict[str, list[str]] = {}
    for info in symbol_tokens.values():
        by_exchange.setdefault(info["exchange"], []).append(info["token"])
    return [
        {"exchangeType": EXCHANGE_TYPE[exchange], "tokens": tokens}
        for exchange, tokens in by_exchange.items()
    ]


def _build_nfo_token_list(option_tokens: dict[str, int]) -> list[dict]:
    if not option_tokens:
        return []
    return [{"exchangeType": EXCHANGE_TYPE["NFO"], "tokens": list(option_tokens.keys())}]
