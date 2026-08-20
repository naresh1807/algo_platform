"""
The actual Angel One SmartWebSocketV2 live-tick connection.

Genuinely a different SmartAPI surface than
apps.market_data.broker_client.BrokerClient's SmartConnect REST calls
(getCandleData/ltpData/placeOrder/...): this opens one persistent
WebSocket, authenticated with the feedToken from BrokerClient's own
login (get_feed_credentials()), and receives a continuous tick stream
instead of polling.

HONESTY NOTE, same spirit as every other broker integration in this
codebase (apps.market_data.broker_client, apps.options.broker_client):
the field names and paise-vs-rupee scaling below are implemented per
Angel One's documented SmartWebSocketV2 QUOTE-mode payload shape.
on_data logs the FIRST raw payload it ever receives at INFO
specifically so this is a 30-second visual check the first time this
runs for real, not a silent assumption -- if field names differ, this
is the only function that needs to change.

TICK-DROP ROOT CAUSE AND FIX (real, observed incident: 5.3M dropped
option ticks across 544 tokens, "option tick queue full" flooding the
log): the option side of this module used to (1) subscribe to EVERY
synced OptionContract for every configured underlying -- every strike,
across every synced expiry, 1000+ live tokens at once (see
apps.options.subscription_manager's own docstring for the fix), and
(2) feed a single fixed-capacity queue.Queue(maxsize=4000) drained by
ONE worker thread doing a DB SELECT + IV solve + DB INSERT per tick.
Under real tick volume that pipeline could never keep up, and a full
queue meant EVERY further tick for EVERY token was unconditionally
dropped. This version narrows the subscription (subscription_manager)
and replaces the queue with a per-token coalescing mailbox
(apps.market_data.tick_coalescer.TickCoalescer) drained by several
worker threads doing only a cheap, DB-free broadcast before any slow
work -- see apps.options.live_feed.handle_option_tick for the fast/slow
split on the other side of that callback.

The SAME queue.Queue(maxsize=1000) + single-worker shape originally
lived on the INDEX side too, on the (wrong) assumption that ~8 index
symbols could never produce enough volume to matter. Observed for real
at market open (09:15-09:20 IST): Angel One bursts several ticks/sec
across all 8 subscribed index symbols simultaneously, and
apps.investing.live_feed.handle_index_tick + CandleAggregator.on_tick
both do real DB work per tick -- "index tick queue full" flooded the
log exactly like the option incident, just on the other pipeline.
Index ticks now go through their own TickCoalescer for the identical
reason: the newest index price is what the ticker/chart needs, not
every intermediate one queued behind a burst.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from django.conf import settings

from . import feed_stats
from .broker_client import BrokerClient
from .market_hours import is_market_open
from .tick_aggregator import CandleAggregator
from .tick_coalescer import TickCoalescer, run_coalescer_workers

logger = logging.getLogger(__name__)

# Angel One's WebSocket feed sends price fields as integers in paise
# (rupees * 100) -- documented SmartWebSocketV2 convention, distinct
# from SmartConnect's REST responses (getCandleData/ltpData), which
# already return rupees.
PAISE_TO_RUPEE = 100

# SmartWebSocketV2's subscribe() exchangeType codes (Angel One's own
# published mapping). NFO (nse_fo) is options; indices live on NSE/BSE
# cash.
EXCHANGE_TYPE = {"NSE": 1, "BSE": 3, "NFO": 2}

# Subscription mode: 2 = QUOTE -- LTP + day OHLC + cumulative day
# volume + previous close in one payload. CandleAggregator needs
# day_volume; apps.investing.live_feed.handle_index_tick needs
# closed_price -- both come free in QUOTE mode from one subscription.
SUBSCRIBE_MODE_QUOTE = 2

# Mode 3 = SNAP_QUOTE -- everything QUOTE gives PLUS open_interest and
# best_5_buy_data/best_5_sell_data (top-of-book price at index [0]).
# Options need this; indices stay on QUOTE.
SUBSCRIBE_MODE_SNAP_QUOTE = 3

_RECONNECT_BACKOFF_INITIAL = 5
_RECONNECT_BACKOFF_MAX = 60
_MARKET_CLOSED_POLL_INTERVAL = 60

# SmartWebSocketV2.connect() calls websocket-client's WebSocketApp.
# run_forever() with no ping_timeout/connect-timeout of its own -- if
# the initial WS handshake itself never completes, run_forever() never
# returns and this codebase's own reconnect/backoff loop never gets a
# chance to run. This bounds how long a single connection ATTEMPT is
# allowed to stay un-opened before being forced closed and treated as a
# failure, via SmartWebSocketV2's own close_connection().
_CONNECT_TIMEOUT_SECONDS = 20


def _dedupe_input_request_dict(sws, mode: int) -> None:
    """
    Angel One's installed SmartWebSocketV2.subscribe() appends to its
    token bookkeeping with list.extend() and never de-duplicates -- if
    this codebase's own diffing (see LiveFeedClient._refresh_subscription)
    ever has to subscribe overlapping tokens across two calls (should not
    happen in normal operation, but must not corrupt state if it does),
    this repairs the resulting list back to unique tokens so a later
    library-internal resubscribe() never sends duplicate entries.
    """
    mode_dict = sws.input_request_dict.get(mode)
    if not mode_dict:
        return
    for exch, tokens in list(mode_dict.items()):
        mode_dict[exch] = list(dict.fromkeys(tokens))


def _repair_input_request_dict_after_unsubscribe(sws, mode: int, token_list: list[dict]) -> None:
    """
    Angel One's installed SmartWebSocketV2.unsubscribe() has a real bug:
    it does `self.input_request_dict.update(request_data)`, merging
    {"correlationID", "action", "params"} directly as TOP-LEVEL keys into
    the SAME dict subscribe() uses as {mode: {exchangeType: [tokens]}}.
    That corrupts the dict's shape -- a later automatic resubscribe()
    (which the library itself triggers internally after a transient
    socket error it retries on its own, see SmartWebSocketV2._on_error)
    iterates `input_request_dict.items()` assuming every value is a
    {exchangeType: [tokens]} dict, and would crash (or resubscribe
    garbage) the moment it hits one of those stray keys.

    Repairs this immediately after every unsubscribe() call: drops the
    stray top-level keys, and removes the just-unsubscribed tokens from
    the real mode/exchangeType structure so a future resubscribe() never
    re-adds a token we just asked the broker to drop.
    """
    for stray_key in ("correlationID", "action", "params"):
        sws.input_request_dict.pop(stray_key, None)
    mode_dict = sws.input_request_dict.get(mode) or {}
    for entry in token_list:
        exch = entry["exchangeType"]
        removed = set(entry["tokens"])
        if exch in mode_dict:
            remaining = [t for t in mode_dict[exch] if t not in removed]
            if remaining:
                mode_dict[exch] = remaining
            else:
                mode_dict.pop(exch, None)


class LiveFeedClient:
    """
    One instance per process (the run_live_feed management command).
    `on_index_tick`, if given, is called for every tick regardless of
    symbol; candle aggregation only runs for settings.WATCHLIST symbols.

    Index and option ticks are processed off the WebSocket's own
    callback thread via their own separate TickCoalescer each (see that
    module's docstring for why a coalescing mailbox, not a FIFO queue,
    is what actually fixes the tick-drop incidents this class's own
    module docstring describes -- both the option one and the index
    one) -- kept as two separate coalescers/worker pools, not one
    shared pool, so a burst on one side can never delay the other.
    """

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
        # A callable (not a static dict): apps.options.subscription_manager
        # .compute_desired_option_tokens, injected here so this module
        # itself never imports Django option models directly (see the
        # original module's own "transport layer only" convention).
        # Called both on every (re)connect AND periodically by
        # _subscription_refresh_loop while already connected.
        self._option_tokens_provider = option_tokens_provider
        self._on_option_tick = on_option_tick
        self._logged_first_tick = False
        # ONE BrokerClient for the process lifetime, reused across
        # reconnects.
        self._broker_client = BrokerClient()
        self._consecutive_failures = 0

        self._index_coalescer = TickCoalescer()
        self._option_coalescer = TickCoalescer()
        self._token_to_symbol: dict[str, str] = {}

        # Guards everything the connection thread (on_open/on_data) and
        # the dynamic subscription-refresh thread both touch: the
        # CURRENTLY-subscribed token -> contract-meta map, and which
        # SmartWebSocketV2 instance (if any) is actually connected right
        # now. `_option_tokens` doubles as the fast broadcast path's
        # contract-identity lookup (apps.options.live_feed.handle_option_tick
        # needs underlying/expiry/strike/option_type without a DB query).
        self._sub_lock = threading.Lock()
        self._option_tokens: dict[str, dict] = {}
        self._active_sws = None
        self._ws_ready = threading.Event()

        # Throttled cross-process observability publishing (see
        # apps.market_data.feed_stats module docstring for why this is
        # throttled rather than published on every tick).
        self._last_index_tick_at: datetime | None = None
        self._last_option_tick_at: datetime | None = None
        self._last_index_publish_monotonic = 0.0
        self._last_option_publish_monotonic = 0.0

        run_coalescer_workers(
            self._index_coalescer, self._handle_index_message,
            settings.LIVE_INDEX_TICK_WORKERS, "live-feed-index",
        )
        run_coalescer_workers(
            self._option_coalescer, self._handle_option_message,
            settings.OPTIONS_LIVE_TICK_WORKERS, "live-feed-options",
        )
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="live-feed-heartbeat").start()
        threading.Thread(target=self._subscription_refresh_loop, daemon=True, name="live-feed-sub-refresh").start()

    def run_forever(self) -> None:
        """
        Blocking. Reconnects with exponential backoff on any error or
        clean close, forever, until the process is killed.
        """
        backoff = _RECONNECT_BACKOFF_INITIAL
        while True:
            is_open, reason = is_market_open()
            if not is_open:
                feed_stats.set_connection_state("market_closed")
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

            feed_stats.set_connection_state("connecting")
            try:
                self._connect_and_subscribe()
                backoff = _RECONNECT_BACKOFF_INITIAL
                self._consecutive_failures = 0
            except Exception as exc:
                logger.exception("Live feed connection dropped -- reconnecting in %ds.", backoff)
                feed_stats.set_connection_state("reconnecting")
                feed_stats.record_error("connection_error", str(exc))
                self._consecutive_failures += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

    def _compute_desired_tokens(self) -> dict[str, dict]:
        if self._option_tokens_provider is None:
            return {}
        try:
            return self._option_tokens_provider() or {}
        except Exception:
            logger.exception("run_live_feed: option_tokens_provider failed -- keeping the previous subscription this cycle.")
            with self._sub_lock:
                return dict(self._option_tokens)

    def _connect_and_subscribe(self) -> None:
        # Imported lazily -- smartapi-python shouldn't be a hard
        # import-time dependency for anything that never runs this.
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        from .broker_client import SYMBOL_TOKENS

        creds = self._broker_client.get_feed_credentials()
        sws = SmartWebSocketV2(
            creds["jwt_token"], creds["api_key"], creds["client_code"], creds["feed_token"],
        )
        # CRITICAL: SmartWebSocketV2.input_request_dict is a CLASS
        # attribute (confirmed by reading the installed package), so
        # every instance mutates the SAME shared dict unless given its
        # own. Without this, subscribe()/unsubscribe() calls across
        # every past and future connection in this process's lifetime
        # would accumulate into one shared, ever-growing, never-reset
        # structure. Giving each fresh connection its own dict makes
        # every reconnect start clean and keeps this module's own
        # subscribe/unsubscribe bookkeeping (_option_tokens) the single
        # source of truth for what THIS connection actually has live.
        sws.input_request_dict = {}

        token_list = _build_token_list(SYMBOL_TOKENS)
        self._token_to_symbol = {info["token"]: symbol for symbol, info in SYMBOL_TOKENS.items()}

        # Freshly resolved right before subscribing -- never the stale
        # result of a previous cycle -- so a reconnect always subscribes
        # the CURRENT expiry/strike-range selection.
        desired = self._compute_desired_tokens()
        with self._sub_lock:
            self._option_tokens = desired

        opened = threading.Event()

        def on_open(wsapp):
            opened.set()
            self._active_sws = sws
            logger.info("Angel One live feed connected -- subscribing to %d symbols.", len(SYMBOL_TOKENS))
            from apps.options import subscription_manager

            sws.subscribe(subscription_manager.make_correlation_id("idx"), SUBSCRIBE_MODE_QUOTE, token_list)
            if desired:
                logger.info("Angel One live feed: subscribing to %d option contracts.", len(desired))
                self._subscribe_tokens(sws, list(desired.keys()))
            self._ws_ready.set()
            feed_stats.set_connection_state("connected")
            feed_stats.set_subscribed_token_count(len(desired))

        def on_data(wsapp, message):
            # Deliberately just routing + a non-blocking handoff here --
            # no DB writes, no IV solving, on this thread. See this
            # module's own class docstring.
            if not self._logged_first_tick:
                self._logged_first_tick = True
                logger.info("run_live_feed: first raw tick payload (verify field names/units against this): %r", message)

            token = message.get("token")
            with self._sub_lock:
                is_option = token in self._option_tokens
            if is_option:
                self._option_coalescer.put(token, message)
            else:
                self._index_coalescer.put(token, message)

        def on_error(category, message):
            # smartapi-python's SmartWebSocketV2 calls this as
            # on_error("Reconnect Error"/"Max retry attempt reached", detail)
            # -- two plain strings, confirmed against the installed package.
            logger.error("Angel One live feed error [%s]: %s", category, message)
            feed_stats.record_error(str(category), str(message))

        def on_close(wsapp):
            logger.warning("Angel One live feed connection closed.")
            self._ws_ready.clear()
            if self._active_sws is sws:
                self._active_sws = None

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
            self._ws_ready.clear()
            if self._active_sws is sws:
                self._active_sws = None

        if not opened.is_set():
            raise TimeoutError(
                f"Angel One live feed: WebSocket connection never opened within {_CONNECT_TIMEOUT_SECONDS}s."
            )

    # ------------------------------------------------------------------
    # Dynamic subscribe/unsubscribe -- the fix for "reload only happens on
    # full reconnect." Runs on its own thread, independent of the blocking
    # connection thread above.
    # ------------------------------------------------------------------

    def _subscribe_tokens(self, sws, tokens: list[str]) -> None:
        from apps.options import subscription_manager

        for chunk in subscription_manager.chunk_tokens(tokens):
            cid = subscription_manager.make_correlation_id("optsub")
            try:
                sws.subscribe(cid, SUBSCRIBE_MODE_SNAP_QUOTE, [{"exchangeType": EXCHANGE_TYPE["NFO"], "tokens": chunk}])
            except Exception:
                logger.exception("live-feed: subscribe() failed for a %d-token chunk (correlation_id=%s).", len(chunk), cid)
        _dedupe_input_request_dict(sws, SUBSCRIBE_MODE_SNAP_QUOTE)

    def _unsubscribe_tokens(self, sws, tokens: list[str]) -> None:
        from apps.options import subscription_manager

        for chunk in subscription_manager.chunk_tokens(tokens):
            cid = subscription_manager.make_correlation_id("optuns")
            token_list = [{"exchangeType": EXCHANGE_TYPE["NFO"], "tokens": chunk}]
            try:
                sws.unsubscribe(cid, SUBSCRIBE_MODE_SNAP_QUOTE, token_list)
            except Exception:
                logger.exception("live-feed: unsubscribe() failed for a %d-token chunk (correlation_id=%s).", len(chunk), cid)
            finally:
                # Repair the library's own bookkeeping regardless of
                # whether the call above raised -- see this module's
                # _repair_input_request_dict_after_unsubscribe docstring.
                _repair_input_request_dict_after_unsubscribe(sws, SUBSCRIBE_MODE_SNAP_QUOTE, token_list)

    def _subscription_refresh_loop(self) -> None:
        interval = settings.OPTIONS_LIVE_SUBSCRIPTION_REFRESH_SECONDS
        while True:
            time.sleep(interval)
            try:
                self._refresh_subscription()
            except Exception:
                logger.exception("live-feed: subscription refresh failed.")

    def _refresh_subscription(self) -> None:
        """
        Periodically re-resolves the desired option-token set and, if the
        feed is currently connected, subscribes only the ADDED tokens and
        unsubscribes only the REMOVED ones on the LIVE connection -- no
        restart, no full resubscribe. If nothing is connected right now,
        this is a no-op: the next successful connect's on_open already
        does a full fresh subscribe from the current desired set.
        """
        sws = self._active_sws
        if sws is None or not self._ws_ready.is_set():
            return

        desired = self._compute_desired_tokens()
        with self._sub_lock:
            current = self._option_tokens
            added = {token: meta for token, meta in desired.items() if token not in current}
            removed = [token for token in current if token not in desired]
            if not added and not removed:
                return
            self._option_tokens = desired

        if added:
            self._subscribe_tokens(sws, list(added.keys()))
            logger.info("live-feed: subscribed %d new option token(s) (dynamic refresh).", len(added))
        if removed:
            self._unsubscribe_tokens(sws, removed)
            logger.info("live-feed: unsubscribed %d option token(s) no longer in range (dynamic refresh).", len(removed))
        feed_stats.set_subscribed_token_count(len(desired))

    def _heartbeat_loop(self) -> None:
        interval = settings.OPTIONS_LIVE_HEARTBEAT_SECONDS
        while True:
            try:
                feed_stats.mark_heartbeat()
                with self._sub_lock:
                    token_count = len(self._option_tokens)
                feed_stats.set_subscribed_token_count(token_count)
                feed_stats.set_option_tick_stats(
                    self._option_coalescer.stats.snapshot(self._option_coalescer.depth())
                )
                feed_stats.set_index_tick_stats(
                    self._index_coalescer.stats.snapshot(self._index_coalescer.depth())
                )
            except Exception:
                logger.exception("live-feed: heartbeat publish failed.")
            time.sleep(interval)

    # ------------------------------------------------------------------
    # Worker loops -- off the WebSocket callback thread.
    # ------------------------------------------------------------------

    def _handle_index_message(self, token: str, message: dict) -> None:
        self._process_index_tick(message)

    def _handle_option_message(self, token: str, message: dict) -> None:
        with self._sub_lock:
            meta = self._option_tokens.get(token)
        if meta is None:
            # Unsubscribed / rolled out from under us between enqueue and
            # processing (benign -- see _refresh_subscription's own note).
            self._option_coalescer.stats.record_rejected()
            return
        self._process_option_tick(meta, message)

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

        self._last_index_tick_at = datetime.now()
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_index_publish_monotonic >= settings.OPTIONS_LIVE_STATS_PUBLISH_INTERVAL_SECONDS:
            self._last_index_publish_monotonic = now_monotonic
            feed_stats.mark_index_tick()

    def _process_option_tick(self, meta: dict, message: dict) -> None:
        if self._on_option_tick is None:
            return

        raw_ltp = message.get("last_traded_price")
        if raw_ltp is None:
            self._option_coalescer.stats.record_rejected()
            return
        ltp = raw_ltp / PAISE_TO_RUPEE

        oi = message.get("open_interest") or 0
        volume = message.get("volume_trade_for_the_day") or 0

        # Top-of-book only (index 0) -- these lists are already the
        # library's OWN best_5_buy_data/best_5_sell_data output (its
        # _parse_binary_data swaps an internal buy/sell pair before
        # returning). Price is paise, same as last_traded_price.
        buy_levels = message.get("best_5_buy_data") or []
        sell_levels = message.get("best_5_sell_data") or []
        bid = (buy_levels[0]["price"] / PAISE_TO_RUPEE) if buy_levels else None
        ask = (sell_levels[0]["price"] / PAISE_TO_RUPEE) if sell_levels else None
        exchange_timestamp_ms = message.get("exchange_timestamp")

        try:
            self._on_option_tick(meta, ltp, oi, volume, bid, ask, exchange_timestamp_ms)
        except Exception:
            logger.exception("on_option_tick failed for contract_id=%s", meta.get("contract_id"))

        self._last_option_tick_at = datetime.now()
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_option_publish_monotonic >= settings.OPTIONS_LIVE_STATS_PUBLISH_INTERVAL_SECONDS:
            self._last_option_publish_monotonic = now_monotonic
            feed_stats.mark_option_tick()


def _build_token_list(symbol_tokens: dict) -> list[dict]:
    by_exchange: dict[str, list[str]] = {}
    for info in symbol_tokens.values():
        by_exchange.setdefault(info["exchange"], []).append(info["token"])
    return [
        {"exchangeType": EXCHANGE_TYPE[exchange], "tokens": tokens}
        for exchange, tokens in by_exchange.items()
    ]
