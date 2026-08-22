"""
Django settings for the algo-trading platform.

Design notes (why things are set up this way):
- All secrets/environment-specific values come from .env (via python-dotenv),
  never hardcoded -- this file is safe to commit.
- MySQL is the system of record per the manual's tech stack. SQLite is
  NOT used anywhere, even for local dev, so schema/behavior stays identical
  across environments (MySQL has different NULL/unique/collation semantics
  than SQLite -- testing against SQLite would hide real bugs).
- Channels (ASGI) runs alongside DRF (WSGI-style views) in the same
  project: ASGI_APPLICATION handles WebSocket routing, DRF handles REST.
- Each domain from the manual's "High-Level Architecture" section is its
  own Django app (apps/market_data, apps/news, apps/signals, apps/risk,
  apps/execution, apps/learning, apps/monitoring, apps/analytics) so that
  risk logic, signal logic, and execution logic stay physically separated
  -- a change to the risk engine can't accidentally reach into execution
  code without an explicit cross-app import.
"""

import os
import sys
from datetime import time as _time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Available early because cache/rate-limit configuration must be isolated in
# tests. DJANGO_TESTING supports runners that do not put test/pytest in argv.
TESTING = (
    os.environ.get("DJANGO_TESTING", "0") == "1"
    or os.environ.get("DJANGO_ENVIRONMENT", "").strip().lower() == "test"
    or (len(sys.argv) > 1 and sys.argv[1] == "test")
    or any(Path(arg).name.lower() in {"pytest", "pytest.exe", "py.test"} for arg in sys.argv)
)

# ---------------------------------------------------------------------------
# Core / security
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]
ENVIRONMENT = os.environ.get("DJANGO_ENVIRONMENT", "development").strip().lower()
_VALID_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})

if ENVIRONMENT not in _VALID_ENVIRONMENTS:
    raise RuntimeError(
        "DJANGO_ENVIRONMENT must be one of: development, test, staging, production."
    )

if ENVIRONMENT == "production" and (
    not SECRET_KEY.strip()
    or len(SECRET_KEY) < 50
    or len(set(SECRET_KEY)) < 5
    or SECRET_KEY == "dev-only-insecure-key-change-me"
    or SECRET_KEY.startswith("django-insecure-")
):
    raise RuntimeError("DJANGO_SECRET_KEY must be a strong secret of at least 50 characters in production.")
if ENVIRONMENT == "production" and DEBUG:
    raise RuntimeError("DJANGO_DEBUG must be 0 in production.")
if ENVIRONMENT == "production" and (
    "DJANGO_ALLOWED_HOSTS" not in os.environ or not ALLOWED_HOSTS
):
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be explicitly configured in production.")

# Transport/cookie hardening is automatic in production while local HTTP
# development remains usable. Proxy deployments must send X-Forwarded-Proto.
SECURE_SSL_REDIRECT = ENVIRONMENT == "production"
SESSION_COOKIE_SECURE = ENVIRONMENT == "production"
CSRF_COOKIE_SECURE = ENVIRONMENT == "production"
SECURE_HSTS_SECONDS = 31536000 if ENVIRONMENT == "production" else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = ENVIRONMENT == "production"
SECURE_HSTS_PRELOAD = ENVIRONMENT == "production"
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if os.environ.get("TRUST_X_FORWARDED_PROTO", "0") == "1"
    else None
)

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    # daphne MUST be first, before django.contrib.staticfiles -- this is
    # what makes `python manage.py runserver` automatically become
    # WebSocket-aware (serving via Channels/ASGI) instead of the plain
    # WSGI dev server, which cannot handle a WebSocket upgrade request
    # at all and instead routes it through normal URL resolution --
    # exactly why /ws/risk/live/ etc. were 404ing as plain HTTP GETs
    # rather than failing as WebSocket connections. daphne itself was
    # already in requirements.txt from the start; it was just never
    # registered here.
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "channels",
    "corsheaders",
    # Platform apps -- one per architectural layer from the manual
    "apps.auth_app",
    "apps.market_data",
    "apps.news",
    "apps.options",
    "apps.signals",
    "apps.risk",
    "apps.execution",
    "apps.learning",
    "apps.paper_trading",
    "apps.monitoring",
    "apps.analytics",
    "apps.admin_tools",
    "apps.jarvis",
    "apps.investing",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",  # must sit above CommonMiddleware
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database -- MySQL only (see module docstring for why)
# ---------------------------------------------------------------------------
# IMPORTANT: your uploaded version had real credentials (username
# "naresh", a real password) hardcoded directly here instead of read
# from .env -- that means they'd get committed to git/shared in this
# file verbatim the moment this project is version-controlled or
# zipped up and sent anywhere (like it just was). Reverted to reading
# from .env; put your actual DB name/user/password/port (3307, matching
# your docker-compose.yml's host-port mapping) in your local .env file
# instead -- .env is already in .gitignore, so it never leaves your
# machine by accident.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("MYSQL_DATABASE", "algo_trading"),
        "USER": os.environ.get("MYSQL_USER", "algo_user"),
        "PASSWORD": os.environ.get("MYSQL_PASSWORD", ""),
        "HOST": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "PORT": os.environ.get("MYSQL_PORT", "3306"),
        "OPTIONS": {
            "charset": "utf8mb4",
        },
    }
}

# ---------------------------------------------------------------------------
# Channels -- real-time layer (live candles, signal updates, risk alerts,
# kill-switch status -- see manual section 10)
# ---------------------------------------------------------------------------
# REAL, OBSERVED INCIDENT: this used to point at the real Redis instance
# UNCONDITIONALLY, with no TESTING-time isolation -- unlike CACHES just
# above, which already had exactly this LocMemCache-under-TESTING split.
# Every apps.*.signals post_save receiver (apps.signals.signals,
# apps.options.signals, apps.investing.signals, ...) broadcasts via
# channel_layer.group_send the moment a model is saved, including from
# test fixtures (e.g. apps/options/tests.py and apps/execution/tests.py
# both create TradingSignal rows with entry_price=Decimal("313.73")/
# Decimal("110") as test data). With this pointed at the real Redis
# instance during `manage.py test`, those fixture saves broadcast REAL
# WebSocket messages to the SAME "signals_live"/"options_live"/
# "market_data_live" groups a real, live browser session is subscribed
# to -- a user with the dashboard open while tests ran saw fake
# "🟢 BUY NIFTY (CE)" signal popups with test-fixture prices, genuinely
# indistinguishable from a real approved signal. InMemoryChannelLayer
# during tests is process-local and torn down with the test process, so
# a test run can never again leak a broadcast into a real user's session.
CHANNEL_LAYERS = (
    {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    if TESTING
    else {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")],
            },
        },
    }
)

# DRF throttling and the option-fetch/cooldown locks must be shared by
# every web process. Django's built-in Redis backend uses the already
# required redis-py dependency; database 2 keeps cache keys isolated from
# the Channels/Celery databases even when REDIS_URL names database 0/1.
def _redis_cache_url() -> str:
    explicit_url = os.environ.get("CACHE_REDIS_URL")
    if explicit_url:
        return explicit_url

    parsed = urlsplit(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    if parsed.scheme not in {"redis", "rediss"} or not parsed.netloc:
        raise RuntimeError("REDIS_URL must be a valid redis:// or rediss:// URL.")
    return urlunsplit(parsed._replace(path="/2"))


CACHES = (
    {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "algo-platform-tests",
        }
    }
    if TESTING
    else {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _redis_cache_url(),
            "KEY_PREFIX": os.environ.get("CACHE_KEY_PREFIX", "algo_platform"),
            "TIMEOUT": 300,
            "OPTIONS": {
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
            },
        },
    }
)

# ---------------------------------------------------------------------------
# Celery -- background jobs (daily review, drift detection, indicator
# recompute, news polling -- anything that must not block a request/response
# cycle or a WebSocket consumer)
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/1")
CELERY_RESULT_BACKEND = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = "Asia/Kolkata"

# ---------------------------------------------------------------------------
# DRF
# ---------------------------------------------------------------------------
try:
    _DRF_NUM_PROXIES = int(os.environ.get("DRF_NUM_PROXIES", "0"))
except ValueError as exc:
    raise RuntimeError("DRF_NUM_PROXIES must be a non-negative integer.") from exc
if _DRF_NUM_PROXIES < 0:
    raise RuntimeError("DRF_NUM_PROXIES must be a non-negative integer.")


REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # TokenAuthentication first: DRF's APIView.get_authenticate_header()
        # only consults authenticators[0] to decide 401 vs 403 for an
        # unauthenticated request. SessionAuthentication.authenticate_header()
        # returns None (no WWW-Authenticate challenge), so with it listed
        # first every unauthenticated API call got 403 instead of 401 --
        # apps.auth_app.tests.test_protected_endpoint_rejects_missing_token
        # is what this platform actually expects (401), and frontend code
        # that treats 401 as "redirect to login" silently broke on 403.
        "common.authentication.ExpiringTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 100,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_THROTTLE_RATES": {
        "login": os.environ.get("LOGIN_THROTTLE_RATE", "5/minute"),
    },
    # Zero is the safe default: use REMOTE_ADDR and do not trust a
    # spoofable X-Forwarded-For header. Set this to the exact number of
    # trusted reverse proxies only when that deployment topology exists.
    "NUM_PROXIES": _DRF_NUM_PROXIES,
}
AUTH_TOKEN_TTL_HOURS = max(1, int(os.environ.get("AUTH_TOKEN_TTL_HOURS", "12")))

# ---------------------------------------------------------------------------
# CORS -- React dev server origin only; tighten before any real deployment
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if origin.strip()
]

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# i18n / tz -- Indian markets, IST throughout so candle timestamps,
# daily-review cutoffs, and cron schedules all agree
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Platform-specific risk constants (manual section 13).
# These are the HARD limits. The learning loop (section 15/21) is only ever
# allowed to tune "soft" parameters elsewhere -- it must NEVER be able to
# write to these. Keeping them in settings (not DB) makes that structurally
# true: no code path exists to update settings.py from a running process.
# ---------------------------------------------------------------------------
RISK_HARD_LIMITS = {
    "MAX_RISK_PER_TRADE_PCT": 1.0,       # never risk more than 1% of equity on one trade
    "MAX_OPEN_RISK_PCT": 6.0,            # sum of open risk across all positions
    "MAX_ONE_SYMBOL_EXPOSURE_PCT": 2.0,
    "MAX_OPEN_POSITIONS": 5,
    "MAX_DAILY_LOSS_PCT": 3.0,
    "MAX_CONSECUTIVE_LOSSES": 3,
    "DRAWDOWN_PAUSE_PCT": 15.0,          # pause new entries
    "DRAWDOWN_FLATTEN_PCT": 20.0,        # flatten everything + halt
    # Option-contract liquidity gate (apps.risk.engine.check_option_contract_liquidity)
    # -- kept in this hard-coded dict, not the DB-backed OptionsStrategySetting,
    # on purpose: these are risk limits (never DB-editable, per this dict's own
    # docstring above), not strategy preferences.
    "MAX_OPTION_BID_ASK_SPREAD_PCT": 5.0,
    "MIN_OPTION_OPEN_INTEREST": 500,
    "MIN_OPTION_VOLUME": 0,               # off by default -- OI is the more
    # meaningful liquidity proxy for a single 5-minute index-option snapshot;
    # raise this if a specific underlying/expiry needs a volume floor too.
}

# Execution rejects approvals that have gone stale while waiting in the
# queue. This is deliberately time-bounded even when a worker was offline;
# it is never safe to submit an old market decision after service recovery.
MAX_SIGNAL_AGE_MINUTES = max(1, int(os.environ.get("MAX_SIGNAL_AGE_MINUTES", "10")))

# Placeholder starting balance for a fresh AccountEquity row (see
# apps/risk/engine.py:get_equity). This is NOT read anywhere else --
# once apps.execution exists it should sync real broker equity into the
# AccountEquity table, at which point this constant only matters for
# brand-new installs / paper-trading resets.
STARTING_EQUITY = float(os.environ.get("STARTING_EQUITY", "100000"))

# Symbols the signal engine (apps/signals/tasks.py) evaluates on its
# schedule. Kept in settings (not a DB table) for now since it's a
# small, rarely-changing personal watchlist -- promote to a model if
# this ever needs to be edited from the dashboard instead of a redeploy.
WATCHLIST = os.environ.get("WATCHLIST", "NIFTY,BANKNIFTY").split(",")

# Underlyings whose signals are generated EXCLUSIVELY by
# apps.options.index_direction_strategy (the option-contract-resolving
# pipeline), not apps.signals.engine.generate_signal (the plain
# index-level engine) -- apps.signals.tasks.generate_signals_for_watchlist
# skips every symbol in this list, and apps.options.index_direction_strategy.
# INDEX_UNDERLYINGS reads this same setting, so neither app imports the
# other's module to agree on which symbols trade via real option contracts.
# Defaults to WATCHLIST's own default since both start out meaning "the
# same two index underlyings" -- override independently if WATCHLIST ever
# grows to include a non-index/equity symbol that should keep using the
# plain engine instead.
OPTIONS_PIPELINE_UNDERLYINGS = os.environ.get(
    "OPTIONS_PIPELINE_UNDERLYINGS", "NIFTY,BANKNIFTY"
).split(",")

# apps.options.expiry_service: the single, shared cutoff used to decide
# whether TODAY's expiry is still eligible to be "the current expiry"
# (before this time) or must roll over to the next listed expiry (at/
# after this time). Defaults to apps.market_data.market_hours.
# MARKET_CLOSE_TIME's own value (15:30 IST, NSE's regular session
# close) but is intentionally its OWN setting, not an import of that
# constant -- an options-specific rollover cutoff is a distinct policy
# decision from "is the equity market open" even though they start out
# equal, and this must be configurable without touching market_hours.py.
# Format: "HH:MM", 24-hour, interpreted in Asia/Kolkata (settings.TIME_ZONE).
_OPTIONS_EXPIRY_CUTOFF_RAW = os.environ.get("OPTIONS_EXPIRY_CUTOFF_TIME", "15:30")
OPTIONS_EXPIRY_CUTOFF_TIME = _time(*(int(p) for p in _OPTIONS_EXPIRY_CUTOFF_RAW.split(":")))

# How many upcoming expiries apps.options.contract_sync keeps synced per
# underlying -- covers signals_engine.select_expiry's CURRENT_WEEK/
# NEXT_WEEK/MONTHLY/CUSTOM modes with real headroom (a monthly expiry
# can be several weekly expiries out). Minimum of 4 per the platform's
# own expiry-lifecycle requirement; defaults to 6 to match this
# codebase's existing SYNC_EXPIRY_COUNT convention.
OPTIONS_EXPIRY_SYNC_COUNT = max(4, int(os.environ.get("OPTIONS_EXPIRY_SYNC_COUNT", "6")))

# Redis distributed-lock TTL (apps.options.sync_lock) guarding contract
# sync/rollover -- long enough to cover a real multi-underlying,
# multi-expiry sync (each expiry is its own Angel One instrument-master
# filter pass, cheap once cached, plus one contract-list fetch per
# expiry), short enough that a crashed holder doesn't wedge the lock for
# an entire trading day.
OPTIONS_SYNC_LOCK_TIMEOUT_SECONDS = int(os.environ.get("OPTIONS_SYNC_LOCK_TIMEOUT_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Live option-feed subscription scope (apps.options.subscription_manager /
# apps.market_data.broker_ws_client) -- see subscription_manager's own module
# docstring for the root-cause incident this fixes: subscribing every synced
# expiry's every strike (1000+ live SNAP_QUOTE tokens) overwhelmed the option
# tick pipeline and produced millions of dropped ticks. These bound the live
# WebSocket subscription to only what the dashboard can actually show.
# ---------------------------------------------------------------------------
# Strikes above/below ATM to keep subscribed, per side (CE+PE) -- 20 is a
# generous default for a single index chain (Angel One index option strikes
# are typically 50-100 points apart, so 20 either side covers a wide real
# trading range) while still cutting subscription count by an order of
# magnitude versus "every strike in every synced expiry."
OPTIONS_LIVE_STRIKE_RANGE = max(0, int(os.environ.get("OPTIONS_LIVE_STRIKE_RANGE", "20")))

# Angel One's SmartWebSocketV2.subscribe()/unsubscribe() accept a token list
# per call; kept well under any documented per-connection cap so a single
# subscribe/unsubscribe request is never oversized, and one bad chunk can
# never fail the whole batch.
OPTIONS_LIVE_MAX_TOKENS_PER_SUBSCRIBE = max(1, int(os.environ.get("OPTIONS_LIVE_MAX_TOKENS_PER_SUBSCRIBE", "50")))

# How often run_live_feed's dynamic subscription manager re-resolves the
# desired option-token set (expiry rollover, operator expiry selection,
# ATM drift) and diffs it against what's currently subscribed -- short
# enough that a rollover or an expiry change from the UI takes effect
# without restarting run_live_feed, long enough to never be a meaningful
# source of broker-request load on its own (this is a local DB read plus a
# small subscribe/unsubscribe delta, not a broker REST call).
OPTIONS_LIVE_SUBSCRIPTION_REFRESH_SECONDS = max(5, int(os.environ.get("OPTIONS_LIVE_SUBSCRIPTION_REFRESH_SECONDS", "15")))

# Worker threads draining apps.market_data.tick_coalescer.TickCoalescer for
# option ticks -- several can run concurrently since the coalescer only ever
# hands a given token to one worker at a time (see that module's docstring).
OPTIONS_LIVE_TICK_WORKERS = max(1, int(os.environ.get("OPTIONS_LIVE_TICK_WORKERS", "4")))

# Same TickCoalescer shape as the option side, applied to the ~8 index
# symbols (apps.market_data.broker_client.SYMBOL_TOKENS) -- observed for
# real that even 8 symbols can burst enough at market open (09:15-09:20
# IST) to overwhelm a single worker doing DB writes per tick
# (apps.investing.live_feed.handle_index_tick + CandleAggregator.on_tick),
# see broker_ws_client's own module docstring. Fewer workers than the
# option side by default since there are far fewer distinct symbols to
# ever have queued up concurrently.
LIVE_INDEX_TICK_WORKERS = max(1, int(os.environ.get("LIVE_INDEX_TICK_WORKERS", "2")))

# How often run_live_feed publishes its heartbeat/stats into the shared
# cache for apps.monitoring's health endpoint (apps.market_data.feed_stats).
OPTIONS_LIVE_HEARTBEAT_SECONDS = max(2, int(os.environ.get("OPTIONS_LIVE_HEARTBEAT_SECONDS", "10")))

# Throttles how often a live index/option tick publishes its "last tick at"
# timestamp into feed_stats -- publishing on EVERY tick would turn the fast,
# in-process tick path into a Redis round-trip per tick, defeating the point
# of the fast path. A couple of seconds of staleness on a HEALTH indicator is
# irrelevant; it is never used to gate the actual LTP broadcast.
OPTIONS_LIVE_STATS_PUBLISH_INTERVAL_SECONDS = max(1, int(os.environ.get("OPTIONS_LIVE_STATS_PUBLISH_INTERVAL_SECONDS", "2")))

# apps.monitoring.health: how stale a heartbeat/last-tick timestamp must be
# before it's reported unhealthy/stale rather than ok.
LIVE_FEED_STALE_SECONDS = max(5, int(os.environ.get("LIVE_FEED_STALE_SECONDS", "45")))
CELERY_HEARTBEAT_STALE_SECONDS = max(30, int(os.environ.get("CELERY_HEARTBEAT_STALE_SECONDS", "150")))

# Where apps.learning.ml_train serializes trained win-probability model
# artifacts (joblib files) -- kept out of the DB / git on purpose, same
# reasoning as ModelRegistry.artifact_path's docstring: the registry
# table stays a lightweight, queryable index, not a blob store. Put
# this on a persistent volume in any real deployment (a container
# restart wiping BASE_DIR would lose trained models, though the
# ModelRegistry row would still exist and just fail to load until
# retrained).
ML_ARTIFACT_DIR = Path(os.environ.get("ML_ARTIFACT_DIR", BASE_DIR / "ml_artifacts"))

# Every candle interval the chart's timeframe selector (frontend
# src/constants/market.js) offers. ingest_watchlist_candles (apps/
# market_data/tasks.py) loops WATCHLIST x CHART_TIMEFRAMES every beat
# tick so that whichever timeframe a user has selected on the chart
# already has fresh HistoricalData rows waiting -- switching the
# dropdown does a REST fetch of existing rows, it never triggers a
# fresh broker call on its own. Keep this list in sync with
# broker_client.TIMEFRAME_TO_ANGEL_INTERVAL (every entry here must have
# a matching Angel One interval mapping) and with the frontend constant.
CHART_TIMEFRAMES = os.environ.get(
    "CHART_TIMEFRAMES", "1m,3m,5m,10m,15m,30m,1h,1d"
).split(",")

# ---------------------------------------------------------------------------
# Broker integration (manual section 4 / Phase 4). BROKER_MODE="paper"
# is the default and the safe starting point per the manual's own
# phased rollout (Phase 3: "Paper trading" comes before Phase 4: "Live
# broker integration") -- apps.market_data/apps.options ingestion tasks
# no-op unless this is "live". Flipping to "live" is a deliberate,
# explicit config change, never a default.
#
# BROKER_MODE only ever gates DATA ingestion (candles, option chain) --
# it does NOT decide whether execution places real orders. That's
# EXECUTION_MODE below, deliberately a separate flag: BROKER_MODE=live
# with EXECUTION_MODE=paper is a fully supported, common combination
# (real market data feeding the signal/learning pipeline, simulated
# fills, zero real-money risk) -- e.g. accumulating enough closed paper
# trades to actually train apps.learning.ml_train's win-probability
# model before ever considering EXECUTION_MODE=live.
# ---------------------------------------------------------------------------
BROKER_MODE = os.environ.get("BROKER_MODE", "paper")
if BROKER_MODE not in {"paper", "live"}:
    raise RuntimeError("BROKER_MODE must be 'paper' or 'live'.")

# Broker rate limits must coordinate across web/Celery processes in real
# operation, while unit tests should stay deterministic and must not wait on
# Redis-backed timing windows.
SMARTAPI_DISTRIBUTED_RATE_LIMIT = os.environ.get(
    "SMARTAPI_DISTRIBUTED_RATE_LIMIT", "0" if TESTING else "1"
) == "1"

# Separate from BROKER_MODE on purpose (see note above) -- this is the
# ONLY flag that controls whether apps.execution.tasks.run_trading_cycle
# calls apps.execution.live_executor (real orders) vs.
# apps.execution.paper_executor (simulated fills against real or paper
# data, per BROKER_MODE). Defaults to "paper" regardless of BROKER_MODE,
# so enabling live data ingestion never silently also enables live order
# placement -- that must always be a second, explicit opt-in, on top of
# the kill switch and real STARTING_EQUITY already required.
EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "paper")
if EXECUTION_MODE not in {"paper", "live"}:
    raise RuntimeError("EXECUTION_MODE must be 'paper' or 'live'.")

# A second, deployment-owned arming gate above the database/UI mode. A user
# cannot turn on real-money execution unless the server operator explicitly
# enabled this environment switch as well.
LIVE_TRADING_ENABLED = os.environ.get("LIVE_TRADING_ENABLED", "0") == "1"

# Deterministic paper-fill friction. These values are snapshotted onto each
# paper OpenPosition when it opens, so changing deployment configuration never
# rewrites an in-flight trade's economics. Option premiums normally have wider
# percentage spreads than liquid cash/underlying prices, hence their separate,
# more conservative defaults. Set both values for a venue to 0 only when an
# explicit frictionless comparison is required.
PAPER_CASH_SLIPPAGE_BPS_PER_SIDE = os.environ.get(
    "PAPER_CASH_SLIPPAGE_BPS_PER_SIDE", "5.0",
)
PAPER_CASH_FEES_BPS_PER_SIDE = os.environ.get(
    "PAPER_CASH_FEES_BPS_PER_SIDE", "5.0",
)
PAPER_OPTION_SLIPPAGE_BPS_PER_SIDE = os.environ.get(
    "PAPER_OPTION_SLIPPAGE_BPS_PER_SIDE", "25.0",
)
PAPER_OPTION_FEES_BPS_PER_SIDE = os.environ.get(
    "PAPER_OPTION_FEES_BPS_PER_SIDE", "10.0",
)

# ---------------------------------------------------------------------
# apps.paper_trading -- the autonomous AI paper-trading subsystem. A
# FULLY SEPARATE vertical from EXECUTION_MODE/apps.execution above (its
# own PaperAccount/PaperOrder/PaperPosition tables, never OpenPosition),
# with its own hard boundaries the AI may never override (see
# apps.paper_trading.services.paper_risk_engine). Angel One is used by
# this subsystem for market data ONLY -- see apps/paper_trading/
# services/angel_one_guard.py for how real order routing is structurally
# impossible from this app, and ALLOW_LIVE_ORDERS below for the second,
# independent config-level guard on top of that.
# ---------------------------------------------------------------------
TRADING_MODE = os.environ.get("TRADING_MODE", "paper")
if TRADING_MODE != "paper":
    raise RuntimeError(
        "TRADING_MODE must be 'paper' -- live order routing is not implemented "
        "or authorized in this codebase. See apps/paper_trading's module docs."
    )
MARKET_DATA_PROVIDER = os.environ.get("MARKET_DATA_PROVIDER", "angelone")
ORDER_EXECUTION_PROVIDER = os.environ.get("ORDER_EXECUTION_PROVIDER", "paper")
if ORDER_EXECUTION_PROVIDER != "paper":
    raise RuntimeError("ORDER_EXECUTION_PROVIDER must be 'paper' in this codebase.")

# Second, independent flag ANDed with LIVE_TRADING_ENABLED inside
# apps.market_data.broker_client.BrokerClient.place_order/cancel_order
# (see that module) -- both must be true for ANY real order to reach
# Angel One. apps.paper_trading's own settings below never enable this;
# it exists so that even a hypothetical future bug that wired a broker
# call into this app would still be blocked at the configuration layer,
# not just by this app structurally never importing broker_client.
ALLOW_LIVE_ORDERS = os.environ.get("ALLOW_LIVE_ORDERS", "false").strip().lower() == "true"

INITIAL_PAPER_CAPITAL = Decimal(os.environ.get("INITIAL_PAPER_CAPITAL", "100000"))

# One-lot / one-position / no-averaging rules (spec's non-negotiable
# trading rules). These are hard boundaries -- apps.paper_trading.
# services.ai_signal_service's model/heuristic policy can select WHICH
# action to take, never override any of these.
LOTS_PER_TRADE = max(1, int(os.environ.get("LOTS_PER_TRADE", "1")))
MAX_CONCURRENT_POSITIONS = max(1, int(os.environ.get("MAX_CONCURRENT_POSITIONS", "1")))
MAX_TRADES_PER_DAY = max(0, int(os.environ.get("MAX_TRADES_PER_DAY", "0")))  # 0 = unlimited
ALLOW_REENTRY = os.environ.get("ALLOW_REENTRY", "true").strip().lower() == "true"
ALLOW_AVERAGING = os.environ.get("ALLOW_AVERAGING", "false").strip().lower() == "true"
ALLOW_PYRAMIDING = os.environ.get("ALLOW_PYRAMIDING", "false").strip().lower() == "true"
ALLOW_MARTINGALE = os.environ.get("ALLOW_MARTINGALE", "false").strip().lower() == "true"
ALLOW_OPTION_SELLING = os.environ.get("ALLOW_OPTION_SELLING", "false").strip().lower() == "true"
if ALLOW_AVERAGING or ALLOW_PYRAMIDING or ALLOW_MARTINGALE or ALLOW_OPTION_SELLING:
    raise RuntimeError(
        "ALLOW_AVERAGING/ALLOW_PYRAMIDING/ALLOW_MARTINGALE/ALLOW_OPTION_SELLING must "
        "all stay false -- apps.paper_trading has no code path implementing any of "
        "these, enabling one would only silently no-op it, not actually allow it."
    )

# Paper-specific risk hard limits (apps.paper_trading.services.
# paper_risk_engine) -- deliberately its own settings block, not reused
# from RISK_HARD_LIMITS above, since that dict governs apps.risk/
# apps.execution's separate real-money-shaped pipeline.
AI_PAPER_DAILY_LOSS_LIMIT_PCT = float(os.environ.get("AI_PAPER_DAILY_LOSS_LIMIT_PCT", "3.0"))
AI_PAPER_MAX_DRAWDOWN_PCT = float(os.environ.get("AI_PAPER_MAX_DRAWDOWN_PCT", "15.0"))
AI_PAPER_MAX_CONSECUTIVE_LOSSES = max(1, int(os.environ.get("AI_PAPER_MAX_CONSECUTIVE_LOSSES", "3")))
AI_PAPER_COOLDOWN_MINUTES = max(1, int(os.environ.get("AI_PAPER_COOLDOWN_MINUTES", "30")))

# Realistic-fill simulation (apps.paper_trading.services.execution_engine).
AI_PAPER_SLIPPAGE_BPS = float(os.environ.get("AI_PAPER_SLIPPAGE_BPS", "5"))
AI_PAPER_SPREAD_FALLBACK_BPS = float(os.environ.get("AI_PAPER_SPREAD_FALLBACK_BPS", "20"))
AI_PAPER_EXECUTION_LATENCY_MS = max(0, int(os.environ.get("AI_PAPER_EXECUTION_LATENCY_MS", "250")))
AI_PAPER_FILL_MODEL_VERSION = os.environ.get("AI_PAPER_FILL_MODEL_VERSION", "v1")
AI_PAPER_STALE_QUOTE_SECONDS = max(5, int(os.environ.get("AI_PAPER_STALE_QUOTE_SECONDS", "120")))

ANGEL_ONE_API_KEY = os.environ.get("ANGEL_ONE_API_KEY", "")
ANGEL_ONE_CLIENT_ID = os.environ.get("ANGEL_ONE_CLIENT_ID", "")
ANGEL_ONE_PASSWORD = os.environ.get("ANGEL_ONE_PASSWORD", "")
ANGEL_ONE_TOTP_SECRET = os.environ.get("ANGEL_ONE_TOTP_SECRET", "")

# Trailing stop-loss (apps/execution/trailing_stop.py). Off by default --
# opt-in, same "never silently change existing behavior" posture as
# every other feature flag in this file. When enabled, a position's
# stop only ever ratchets UP as price makes a new high since entry,
# trailing by the SAME distance as the initial ATR-based stop
# (entry_price - stop_loss at open) -- no separate tunable distance,
# so there's one less parameter to get wrong. The stop still never
# moves down, matching how apps.risk.engine already treats a position's
# risk as fixed once sized.
TRAILING_STOP_ENABLED = os.environ.get("TRAILING_STOP_ENABLED", "0") == "1"

# News/sentiment integration (manual section 4). Empty by default --
# apps.news.tasks.poll_news_sources logs a warning and skips rather
# than erroring when this is blank, same pattern as BROKER_MODE=paper
# skipping candle ingestion.
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")

# manual section 16: "Encrypt secrets." Used by common/crypto.py for
# any model field that needs to store a secret in the DB (none do yet
# -- see that module's docstring). Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FIELD_ENCRYPTION_KEY = os.environ.get("FIELD_ENCRYPTION_KEY", "")

# apps.jarvis.local_llm -- a self-hosted, open-source LLM (via Ollama,
# https://ollama.com, running as a separate local process this Django
# app only ever talks to over plain HTTP on localhost) used ONLY as
# JARVIS's fallback for free-form questions that apps.jarvis.intent's
# rule-based matcher doesn't recognize. Deliberately NOT a paid external
# API (OpenAI/Anthropic/etc.) -- no per-call cost, no data leaving the
# machine, consistent with this project's "self-host what you can"
# posture (apps.news.rss_client over NewsAPI, Angel One's own instrument
# master over nseindia.com scraping). If Ollama isn't running locally,
# apps.jarvis.local_llm.ask() fails soft (returns None, logged, not
# raised) -- same "missing integration is a skip, not a crash"
# convention as NEWSAPI_KEY/BROKER_MODE elsewhere in this file.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")

# ---------------------------------------------------------------------------
# Logging -- explicit config (Django's implicit default was fine for dev but
# gave no place to hook a redaction filter) so that a log line built from an
# exception's str(), a raw broker response, or an HTTP header dict can never
# leak an Authorization header, JWT, API key, password, or TOTP secret into
# a log file. common.log_filters.RedactSensitiveDataFilter scans every
# formatted record for a small set of known-sensitive patterns and replaces
# the value with "***REDACTED***" -- applied to every handler below, not
# just one, so nothing bypasses it by using a different logger name.
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_sensitive": {
            "()": "common.log_filters.RedactSensitiveDataFilter",
        },
    },
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["redact_sensitive"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
    },
}
