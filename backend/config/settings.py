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
from datetime import time as _time
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Core / security
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

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
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")],
        },
    },
}

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
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 100,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
}

# ---------------------------------------------------------------------------
# CORS -- React dev server origin only; tighten before any real deployment
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",")

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

# Separate from BROKER_MODE on purpose (see note above) -- this is the
# ONLY flag that controls whether apps.execution.tasks.run_trading_cycle
# calls apps.execution.live_executor (real orders) vs.
# apps.execution.paper_executor (simulated fills against real or paper
# data, per BROKER_MODE). Defaults to "paper" regardless of BROKER_MODE,
# so enabling live data ingestion never silently also enables live order
# placement -- that must always be a second, explicit opt-in, on top of
# the kill switch and real STARTING_EQUITY already required.
EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "paper")

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
