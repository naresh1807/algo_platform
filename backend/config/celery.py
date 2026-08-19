"""
Celery app for background jobs: news polling, indicator recompute, the
daily learning loop (manual section 15), drift detection, and anything
else that must run off the request/response and WebSocket-consumer paths.
"""

import os
import platform

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("algo_trading")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

if platform.system() == "Windows":
    # Prefork worker pools can fail on Windows with billiard handle and
    # semaphore permission errors. Use the solo pool for local Windows
    # development, or pass --pool=threads for thread-based concurrency.
    app.conf.worker_pool = "solo"
    app.conf.worker_concurrency = 1

# --pool=solo means concurrency=1: ONE worker process can only ever run
# ONE task at a time, full stop -- there is no in-process parallelism to
# fall back on. Observed for real: ingest_index_chart_candles (looping
# many symbol x timeframe combinations, each individually retrying with
# backoff when Angel One is rate-limited) took over 6 minutes end to end
# under real rate-limit pressure, which on a single default-queue worker
# completely starves every OTHER scheduled task queued behind it for
# that whole window -- including ingest_option_chain_snapshots (every 5
# minutes), so the option chain's own displayed prices silently stopped
# updating even though nothing about ingest_option_chain_snapshots
# itself was broken. Routing it onto its own "priority" queue means a
# second worker process consuming ONLY that queue (see the
# "Running the project" note in this repo's own docs/README for the
# exact second `celery worker -Q priority` command) can always run it
# immediately, independent of however long a slow candle-ingestion task
# on the default queue is taking. Scoped to just this one task
# deliberately, not every options/market-data task -- broadening this
# indiscriminately would just recreate the same starvation risk one
# queue over.
app.conf.task_routes = {
    "apps.options.tasks.ingest_option_chain_snapshots": {"queue": "priority"},
}

# Scheduled jobs (Celery beat). Times are IST (settings.CELERY_TIMEZONE).
# NOTE on ordering: ingest-watchlist-candles and generate-signals both
# run every 5 minutes as independent schedules, so there's no hard
# guarantee ingestion finishes before signal generation reads the
# candles it just fetched on any given tick -- generate_signal()
# tolerates this fine (it just sees slightly stale data that gets
# picked up next cycle), but if this ever needs a strict ordering,
# use a Celery chain/chord here instead of two separate beat entries.
app.conf.beat_schedule = {
    "ingest-watchlist-candles-every-5-minutes": {
        "task": "apps.market_data.tasks.ingest_watchlist_candles",
        "schedule": 300.0,
    },
    "ingest-watchlist-1m-every-minute": {
        "task": "apps.market_data.tasks.ingest_watchlist_candles",
        "schedule": 60.0,
        "args": ("1m",),
    },
    # Chart-only indices (NIFTY IT/AUTO/FMCG/PHARMA, SENSEX) -- NOT part
    # of settings.WATCHLIST, so this is a separate task/schedule entry
    # from the one above (see ingest_index_chart_candles's own
    # docstring for why). 10 minutes, not 5 -- these are display-only,
    # never read by apps.signals/apps.risk, so there's no reason to
    # pull them at the same cadence as the actually-traded symbols and
    # double the Angel One call volume for no functional benefit.
    "ingest-index-chart-candles-every-10-minutes": {
        "task": "apps.market_data.tasks.ingest_index_chart_candles",
        "schedule": 600.0,
    },
    "ingest-option-chain-every-5-minutes": {
        "task": "apps.options.tasks.ingest_option_chain_snapshots",
        "schedule": 300.0,
    },
    # Expiry-rollover lifecycle (apps.options.expiry_service /
    # apps.options.contract_sync) -- four cooperating jobs, replacing
    # the old single weekly-Monday sync, which proved too infrequent:
    # a mid-week expiry rollover (or a single failed Monday run) could
    # leave the platform offering an already-expired contract for days
    # with nothing to notice or recover. See apps.options.expiry_service's
    # own module docstring for the root-cause incident this fixes.
    #
    # 1) Force a fresh instrument-master download before market open,
    #    so today's sync always sees today's listing (including any
    #    exchange-holiday-shifted expiry Angel One published overnight).
    "refresh-instrument-master-before-market-open": {
        "task": "apps.options.tasks.refresh_instrument_master",
        "schedule": crontab(hour=8, minute=45, day_of_week="1-5"),
    },
    # 2) Daily (not weekly) contract sync, right after the master
    #    refresh above -- an already-listed expiry's own strikes/tokens
    #    rarely change mid-week (see OptionContract's docstring), but
    #    which expiry counts as "nearest"/"current" changes every time
    #    one expires, so this needs to run daily to catch that reliably.
    "sync-option-contracts-daily": {
        "task": "apps.options.tasks.sync_watchlist_option_contracts",
        "schedule": crontab(hour=8, minute=50, day_of_week="1-5"),
    },
    # 3) Shortly after settings.OPTIONS_EXPIRY_CUTOFF_TIME (default
    #    15:30 IST) -- re-syncs only the underlyings that actually need
    #    it (apps.options.expiry_service.rollover_required), so today's
    #    just-expired contract stops being offered the same afternoon
    #    it expires, not at the next day's pre-market sync.
    "expiry-rollover-after-cutoff": {
        "task": "apps.options.tasks.rollover_expiries",
        "schedule": crontab(hour=15, minute=35, day_of_week="1-5"),
    },
    # 4) Lightweight, DB-only health check (apps.options.expiry_service.
    #    rollover_required -- no Angel One call) every 15 minutes,
    #    around the clock every day (not just market hours/weekdays) --
    #    this is the SELF-HEAL path: if Celery Beat was stopped across
    #    the scheduled rollover above (or the pre-market sync failed),
    #    the very next tick after Beat comes back notices and triggers
    #    a real resync on its own, no human intervention required.
    "options-sync-health-check-every-15-minutes": {
        "task": "apps.options.tasks.options_sync_health_check",
        "schedule": crontab(minute="*/15"),
    },
    "poll-news-every-5-minutes": {
        "task": "apps.news.tasks.poll_news_sources",
        "schedule": 300.0,
    },
    # Beat fires this every 5 minutes around the clock, but the task
    # itself (apps.signals.tasks.generate_signals_for_watchlist) now
    # skips as a no-op outside NSE market hours via
    # apps.market_data.market_hours.is_market_open -- gating here in
    # beat_schedule instead would need crontab(minute="*/5", hour="9-15",
    # day_of_week="1-5") AND still wouldn't know about holidays, so the
    # gate lives in the task itself instead (one place, reused by
    # anything else that needs "is the market open" -- see that
    # module's own docstring on its holiday-list honesty caveat).
    "generate-signals-every-5-minutes": {
        "task": "apps.signals.tasks.generate_signals_for_watchlist",
        "schedule": 300.0,
    },
    "trading-cycle-every-5-minutes": {
        "task": "apps.execution.tasks.run_trading_cycle",
        "schedule": 300.0,
    },
    # apps.options.index_direction_strategy: NIFTY/BANKNIFTY direction
    # -> CE/PE side -> that side's backtest success rate -> trade.
    # Same 5-minute/market-hours-gated-in-task cadence as
    # generate-signals-every-5-minutes above, and its APPROVED signals
    # feed the SAME trading-cycle-every-5-minutes execution task (no
    # separate execution task needed -- run_trading_cycle picks up any
    # APPROVED BUY/SELL signal regardless of which module created it).
    "index-direction-strategy-every-5-minutes": {
        "task": "apps.options.tasks.run_index_direction_strategy",
        "schedule": 300.0,
    },
    "daily-review-after-market-close": {
        "task": "apps.learning.tasks.run_daily_review",
        "schedule": crontab(hour=16, minute=0),  # 4:00 PM IST, after NSE close
    },
    "drift-check-every-hour": {
        "task": "apps.learning.tasks.check_for_drift",
        "schedule": 3600.0,
    },
    "ml-model-performance-check-daily": {
        "task": "apps.learning.tasks.monitor_ml_model_performance",
        "schedule": crontab(hour=16, minute=15),  # just after daily-review, 4:15 PM IST
    },
    # Daily, right after monitor_ml_model_performance -- per the user's
    # explicit "the model must relearn from every closed trade's actual
    # outcome every day" requirement. train_win_probability_model()
    # itself stays a safe no-op below MIN_TRAINING_SAMPLES closed trades
    # (apps.learning.ml_train), and its champion/challenger gate still
    # blocks a genuinely worse challenger from replacing a better
    # champion -- so running it daily instead of weekly only means "check
    # for an update more often," never "accept a worse model just
    # because a day passed."
    "retrain-win-probability-model-daily": {
        "task": "apps.learning.tasks.retrain_win_probability_model",
        "schedule": crontab(
            hour=16, minute=30
        ),  # 4:30 PM IST, after the daily review/ML-monitor pair
    },
    # apps.learning.ml_technical's technical-direction model (separate
    # from the win-probability model above -- see that module's
    # docstring) trains on tens of thousands of historical candle rows
    # per symbol/timeframe, not closed trades, so it doesn't need a
    # daily cadence -- weekly, well outside market hours, since each
    # symbol/timeframe combo's HistGradientBoostingClassifier fit is
    # meaningfully heavier than the win-probability model's logistic
    # regression. Previously this model existed with no scheduled
    # caller at all (trained only via a manual shell invocation) --
    # this is what actually makes it a running part of the platform.
    "retrain-technical-direction-models-weekly": {
        "task": "apps.learning.tasks.retrain_technical_direction_models",
        "schedule": crontab(hour=2, minute=0, day_of_week="0"),  # Sunday 2 AM IST
    },
    # Multi-method paper-trading comparison (trend-following/mean-
    # reversion/breakout, apps.learning.strategy_methods) -- same 5-min
    # cadence as generate-signals-every-5-minutes since it's also
    # market-hours-gated inside the task itself (is_market_open), not
    # here in beat_schedule, same reasoning as that task's own comment
    # above. Fully isolated from the real strategy/execution/risk state
    # -- see apps/learning/models.py's HypotheticalTrade docstring.
    "strategy-method-comparison-every-5-minutes": {
        "task": "apps.learning.tasks.run_strategy_method_comparison",
        "schedule": 300.0,
    },
    # Daily "which method is winning" write to ModelRegistry (the "ML
    # memory" the trader asked for) -- right after retrain-win-
    # probability-model-daily (16:30), same end-of-day learning-loop
    # slot as the rest of this block.
    "evaluate-strategy-methods-daily": {
        "task": "apps.learning.tasks.evaluate_strategy_methods",
        "schedule": crontab(hour=16, minute=45),
    },
    # Scalping comparison group (ema-momentum/rsi-extreme/sar-volume-
    # burst, apps.learning.strategy_methods.SCALPING_METHOD_FUNCS) --
    # every 1 minute, matching ingest-watchlist-1m-every-minute's own
    # cadence and SCALPING_COMPARISON_MAX_HOLDING_BARS's short (12-bar)
    # holding window: checking a scalp's stop/target only every 5
    # minutes would defeat the point of scalping. Same market-hours
    # gate inside the task, same full isolation from real execution/
    # risk as the swing comparison above.
    "scalping-strategy-comparison-every-minute": {
        "task": "apps.learning.tasks.run_scalping_strategy_comparison",
        "schedule": 60.0,
    },
    # apps.learning.scalp_execution's REAL (paper-mode) option-position
    # path for the same three scalping methods -- an ADDITIONAL entry
    # alongside the isolated comparison above, not a replacement; same
    # 1-minute cadence for the same "scalping needs sub-5-minute checking"
    # reason. See that module's own docstring for the one-open-position-
    # per-symbol consequence of running this alongside
    # index-direction-strategy-every-5-minutes below.
    "scalping-real-execution-every-minute": {
        "task": "apps.learning.tasks.run_scalping_real_execution",
        "schedule": 60.0,
    },
    "evaluate-scalping-strategy-methods-daily": {
        "task": "apps.learning.tasks.evaluate_scalping_strategy_methods",
        "schedule": crontab(hour=16, minute=50),  # right after evaluate-strategy-methods-daily
    },
    # apps.learning.scalp_ml_train's own win-probability model, trained
    # ONLY on closed scalping HypotheticalTrade rows (see that module's
    # docstring for why it's a separate model from retrain-win-
    # probability-model-daily's) -- right after the scalping methods'
    # daily win-rate/avg-R evaluation above, same "once the day's scalps
    # are all accounted for" timing that task already uses.
    "retrain-scalp-win-probability-model-daily": {
        "task": "apps.learning.tasks.retrain_scalp_win_probability_model",
        "schedule": crontab(hour=17, minute=0),
    },
    # manual 14.16 "JARVIS Announces": Market Open / Market Close. NSE's
    # regular session is 09:15-15:30 IST, Mon-Fri -- day_of_week='1-5'
    # (Celery: 0=Sunday) skips weekends. NSE trading holidays will still
    # fire a (harmless) false announcement since this scaffold has no
    # holiday calendar -- see apps.jarvis.tasks module docstring.
    "announce-market-open": {
        "task": "apps.jarvis.tasks.announce_market_open",
        "schedule": crontab(hour=9, minute=15, day_of_week="1-5"),
    },
    "announce-market-close": {
        "task": "apps.jarvis.tasks.announce_market_close",
        "schedule": crontab(hour=15, minute=30, day_of_week="1-5"),
    },
    # AI/ML Market Intelligence -- the trader-requested "ML/JARVIS
    # continuously observes stocks/options/index/news" synthesis (see
    # apps.jarvis.market_intelligence's own module docstring on what
    # this does and does NOT do). Every 15 minutes during market hours
    # only -- outside market hours the underlying signals/news don't
    # meaningfully change, so there's nothing new to synthesize.
    "generate-market-outlook-every-15-minutes": {
        "task": "apps.jarvis.tasks.generate_market_outlook",
        "schedule": crontab(minute="*/15", hour="9-15", day_of_week="1-5"),
    },
    # manual 14.15/14.16: nightly database backup + its completion
    # announcement. 11 PM IST -- well after market close and the daily
    # review/ML pipeline above, so the backup captures a fully-settled
    # day and mysqldump isn't competing with those jobs for DB load.
    "database-backup-nightly": {
        "task": "apps.admin_tools.tasks.run_database_backup",
        "schedule": crontab(hour=23, minute=0),
    },
    # Trader-facing feature, not manual-specified (see apps.monitoring.
    # models.PriceAlert's docstring) -- every 2 minutes is frequent
    # enough that a triggered alert announces close to when it actually
    # happened, without adding meaningful load (one cheap query per
    # active alert against data already being ingested anyway).
    "check-price-alerts-every-2-minutes": {
        "task": "apps.monitoring.tasks.check_price_alerts",
        "schedule": 120.0,
    },
    # Long-term stock investing (apps.investing) -- fundamentals are
    # quarterly, not intraday, so weekly is more than frequent enough.
    # Ordered so recommendations are generated right after fresh
    # fundamentals land, not against stale data from last week.
    "refresh-watchlist-fundamentals-weekly": {
        "task": "apps.investing.tasks.refresh_watchlist_fundamentals",
        "schedule": crontab(
            hour=6, minute=0, day_of_week="6"
        ),  # Saturday, well outside market hours
    },
    # The broker-app-style index ticker cards -- genuinely "live-ish"
    # unlike the rest of apps.investing (which is weekly/quarterly).
    # Every 3 minutes during market hours (mirrors
    # check-price-alerts-every-2-minutes's own market-hours-only
    # reasoning -- no point hammering NSE's API when the market, and
    # therefore the index price, isn't moving).
    "sync-index-prices-every-3-minutes": {
        "task": "apps.investing.tasks.sync_index_constituents_and_prices",
        "schedule": crontab(minute="*/3", hour="9-15", day_of_week="1-5"),
    },
    "refresh-watchlist-price-history-weekly": {
        "task": "apps.investing.tasks.refresh_watchlist_price_history",
        "schedule": crontab(hour=6, minute=15, day_of_week="6"),
    },
    "generate-stock-recommendations-weekly": {
        "task": "apps.investing.tasks.generate_stock_recommendations",
        "schedule": crontab(hour=6, minute=30, day_of_week="6"),
    },
    "sync-ipo-calendar-daily": {
        "task": "apps.investing.tasks.sync_ipo_calendar",
        "schedule": crontab(
            hour=7, minute=0
        ),  # IPO windows open/close on their own daily cadence, unlike quarterly results
    },
}
