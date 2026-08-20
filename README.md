# Algo Trading Platform — Phase 1 Scaffold

This is the **Phase 1** deliverable from the deployment plan in your
manual: data-ingestion schema, MySQL models, and a React UI shell. It is
a real, runnable skeleton — every model, endpoint, and page listed below
actually works — but the trading logic itself (indicator engine,
sentiment engine, signal engine, risk engine, broker integration,
learning loop) is deliberately left as documented stubs. Building those
is Phases 2-5, and each one deserves its own careful pass rather than
being rushed here.

## What's actually implemented right now

**Backend**
- Django project with 9 apps, one per architectural layer from the
  manual (`market_data`, `news`, `signals`, `risk`, `execution`,
  `learning`, `monitoring`, `analytics`, plus `auth_app`)
- Every table from the manual's schema (section 7) as a real Django
  model, in MySQL, with the relationships between them (e.g. a
  `TradingSignal` links to the `StrategyVersion` that produced it, an
  `OpenPosition` links back to its `TradingSignal`)
- A working DRF API over all of it (list/retrieve for most things;
  full CRUD on `StrategyVersion` and `DailyReviewNote` since those are
  the two things a human is meant to edit directly)
- `settings.RISK_HARD_LIMITS` — the non-negotiable limits from section
  13, deliberately kept as Python constants (not DB rows), so there is
  no code path that lets the automated learning loop weaken them
- Celery + Channels wired up and pointed at the right places
- **Phase 2, now implemented end-to-end:**
  - `apps/market_data/indicators.py` — EMA9/21 (+ slopes), MACD/signal/
    histogram, RSI, ATR, ADX, Bollinger width, Parabolic SAR, relative
    volume, and the "closed below EMA9 for N candles" streak counter,
    all via pandas-ta against `HistoricalData`
  - `apps/market_data/regime.py` — trending / sideways / high-volatility
    classification from ADX + Bollinger width + ATR expansion
  - `apps/news/scoring.py` — recency-weighted sentiment aggregation per
    symbol, plus the "contradictory headline" veto flag
  - `apps/risk/engine.py` — the actual pre-trade risk gate: kill-switch
    check, drawdown pause/flatten, daily loss limit, consecutive-loss
    cooldown, max-open-positions/single-symbol-exposure checks, and
    ATR-based position sizing that scales down as drawdown grows.
    Also owns the *only* function allowed to flip the kill switch on.
  - `apps/signals/engine.py` — the composite scoring engine tying all
    of the above together into a saved `TradingSignal` row every time
    it runs, whether the outcome is BUY or NO_TRADE, approved or
    rejected — nothing here has a silent "give up and return None" path
  - `apps/risk/models.AccountEquity` — the equity/drawdown/
    consecutive-losses tracking the risk engine reads from, exposed on
    the dashboard via `/api/risk/equity/`
  - Celery beat now runs `generate_signals_for_watchlist` every 5
    minutes across `settings.WATCHLIST`

**Frontend**
- React + Vite dashboard shell matching section-18 requirements
- Signals / Positions / Risk Log (now shows live equity + drawdown) /
  Learning pages

- **Phase 3, now implemented end-to-end:**
  - `apps/market_data/broker_client.py` — Angel One SmartAPI wrapper
    (lazy-imported, TOTP login, historical candle fetch, feed-health
    probe). Only actually connects when `BROKER_MODE=live`
  - `apps/market_data/tasks.py` — recurring candle ingestion into
    `HistoricalData` (idempotent upsert via `unique_together` +
    `ignore_conflicts=True`), plus writes `FeedHealthCheck` rows
  - `apps/execution/paper_executor.py` — the paper-trading engine:
    opens an `OpenPosition` from an approved `TradingSignal`, marks the
    signal `EXECUTED`, and closes positions on stop-loss/target hits or
    a technical exit from `apps.signals.engine.should_exit_position` —
    this is the *only* code that writes to `AccountEquity.current_equity`
  - `apps/execution/tasks.py` — the paper-trading cycle, scheduled
    right after signal generation
  - `BROKER_MODE` setting (`paper` by default, per the manual's own
    Phase 3-before-Phase-4 ordering) — both new tasks no-op with a log
    line unless explicitly switched

- **Phase 4, now implemented:**
  - `apps/risk/engine.py` — feed-freshness is now part of the pre-trade
    gate (`_check_feed_freshness`, reading the latest `FeedHealthCheck`
    row with a 15-minute staleness threshold), closing the gap flagged
    after Phase 3
  - `apps/risk/management/commands/rearm_kill_switch.py` — the
    deliberately-manual-only way to deactivate a tripped kill switch
    (`python manage.py rearm_kill_switch --by "you" --confirm`). There
    is still no REST endpoint anywhere that can reactivate it — that's
    intentional, not an oversight
  - `apps/learning/tasks.py:check_for_drift` — now actually compares
    the active `StrategyVersion`'s recorded `baseline_metrics.win_rate`
    against a rolling win-rate from its own closed trades, and logs a
    `DriftEvent` (never an automatic rollback — only ever
    `action_taken="recommended_rollback"`)
  - `apps/market_data/broker_client.py` — added `place_order`,
    `get_order_status`, `get_ltp` (order placement was previously only
    historical-candle fetching)
  - `apps/execution/live_executor.py` — real order placement mirroring
    `paper_executor.py`'s exact structure and equity-update discipline,
    with fill-confirmation polling (never assumes an instant fill) and
    clear failure handling (a failed/unfilled order rejects the signal
    rather than silently marking it executed)
  - `apps/execution/tasks.py` — `run_trading_cycle` now routes to
    paper or live based on `BROKER_MODE`, with a kill-switch check as
    defense-in-depth on top of the one already in the risk engine

- **Phase 5, now implemented (with an honesty caveat below):**
  - `apps/analytics/services.py:compute_daily_performance` — computes a
    real `PerformanceMetrics` row per day from actual closed trades
    (win rate, profit factor, expectancy/avg-R computed per-trade from
    each position's own ATR-based stop distance, false-signal rate from
    that day's rejected signals). `max_drawdown` and `slippage` are left
    `None` on purpose — both need data this scaffold doesn't collect
    yet (an intraday equity curve; a paper-vs-live fill-price
    comparison), and reporting a fabricated number would be worse than
    reporting nothing
  - `apps/learning/tasks.py:run_daily_review` — drafts a
    `DailyReviewNote` from the day's real performance, with two simple,
    transparent heuristics for `suggested_changes` (loosen the
    technical-score threshold if rejections cluster just below it;
    reconsider the ATR stop multiplier if win rate is poor on a
    reasonable sample). Always writes the note with
    `approved_flag=False`, never touches `StrategyVersion`
  - `apps/learning/management/commands/apply_review_changes.py` — the
    explicit, separate step that turns an *approved* review into a
    brand-new `StrategyVersion` (never mutates the active one, never
    sets it active). Promoting that new version is a further, separate
    step via the existing `StrategyVersion` API/admin — four distinct
    deliberate actions in total between "a heuristic noticed something"
    and "a new strategy version is actually live," matching the
    manual's "never auto-change core safety rules / never deploy
    unvalidated changes" rules

**Honesty caveat on Phase 5**: `run_daily_review`'s suggested_changes
logic is real code that runs against real numbers, but the two
heuristics in it are simple rules of thumb, not validated strategy —
they exist to demonstrate the *mechanism* (draft -> approve -> apply ->
promote, each a separate gate) working correctly end-to-end, not to be
trusted as good trading advice on day one. Section 19's backtesting
standards (walk-forward validation, out-of-sample testing, regime-
specific analysis) are the right tool for actually deciding what
`suggested_changes` should contain, and none of that exists in this
scaffold yet.

- **WebSocket consumers, now implemented** (previously the biggest gap
  left over from the initial scaffold):
  - `apps/market_data/consumers.py` + `apps/market_data/signals.py` —
    every newly-ingested `HistoricalData` row broadcasts to a shared
    `market_data_live` group via a `post_save` signal, so ANY code path
    that creates a candle (the recurring ingestion task, a manual
    backfill, a shell one-off) pushes live updates automatically
  - `apps/signals/consumers.py` + `apps/signals/signals.py` — every new
    `TradingSignal` broadcasts, including rejected/NO_TRADE ones, so
    the dashboard's live signal panel reflects every evaluation, not
    just approved trades
  - `apps/risk/consumers.py` + `apps/risk/signals.py` — kill-switch
    state changes and CRITICAL risk events broadcast live (warning/info
    events stay REST-only in the Risk Log page, deliberately not
    interrupting the live view)
  - `apps/monitoring/signals.py` — feed-health checks broadcast into
    the *same* `risk_live` group (not a separate monitoring socket) --
    tagged `type: "feed_health"` — since "is the feed healthy" is
    conceptually part of "is it safe to trade right now"
  - Frontend: `Dashboard.jsx` now actually appends live-pushed candles
    into the chart (filtered to the symbol/timeframe being displayed),
    not just the REST-fetched history

- **Correlation-based exposure check, now implemented** (closes a gap
  flagged since Phase 2):
  - `apps/market_data/correlation.py` — Pearson correlation matrix over
    daily returns (not raw price levels — see the module docstring for
    why that distinction matters) across `settings.WATCHLIST`
  - `apps/risk/engine.py:_check_correlated_exposure` — blocks a new
    entry if a highly-correlated symbol already has an open position,
    treating it as concentrating risk rather than diversifying.
    Deliberately a simple block-on-any-overlap check rather than a
    weighted combined-exposure percentage — the more precise version
    needs real beta/volatility weighting that isn't built, and isn't
    justified yet at a 2-symbol watchlist

- **News sentiment (FinBERT + NewsAPI), now implemented** (closes the
  last major stub from the original scaffold):
  - `apps/news/newsapi_client.py` — fetches headlines per watchlist
    symbol via NewsAPI.org, with a static query-string map
    (`SYMBOL_NEWS_QUERIES`) since NewsAPI doesn't know what "NIFTY"
    means as a ticker — same caveat as the broker's `SYMBOL_TOKENS`:
    review what it actually returns before trusting it
  - `apps/news/finbert.py` — wraps `ProsusAI/finbert` (a model actually
    fine-tuned on financial text, not a general-purpose sentiment
    model that routinely gets financial language backwards). Lazy-loaded
    on first real use, same pattern as the broker client, so importing
    this module doesn't force a multi-second model load in processes
    that never score anything
  - `apps/news/tasks.py:poll_news_sources` — ties both together per
    symbol, dedups on (symbol, source, published_at) so the deliberate
    6-hour lookback overlap doesn't double-score the same article, and
    skips cleanly (not an error) if `NEWSAPI_KEY` is blank
  - `apps/news/scoring.py`'s sentiment aggregation, dormant since Phase
    2, finally has real data to consume

- **Options Intelligence, now implemented** (manual v2, section 9 —
  the first piece of the expanded options-first/voice/RBAC manual):
  - `apps/options/models.py` — `OptionContract` (static per-strike
    reference data) and `OptionChainSnapshot` (the time-series OI/IV/
    volume/price data), split the same way `HistoricalData` separates
    static from time-series concerns in `apps.market_data`
  - `apps/options/metrics.py` — PCR, max pain (from the actual
    settlement-payout definition, not an approximation), IV rank, and
    strike-wise support/resistance — all pure functions, independently
    testable against fixture data
  - `apps/options/signals_engine.py` — every signal from the manual's
    options-signal list (bullish/bearish buildup, short-covering, long
    unwinding, expiry pinning, IV crush warning, high-decay zone), each
    returned with a plain-text reason, matching the manual's "AI must
    explain every signal" principle applied at the options layer
  - `apps/risk/engine.py` — expiry-day position-size reduction (manual
    section 12), stacking multiplicatively with the existing
    drawdown-based reduction rather than overriding it
  - Frontend: `OptionsAnalytics.jsx`, one of the manual's must-have
    pages, calling a single bundled `/api/options/analytics/` endpoint

**Honest gap in this piece**: `apps/options/broker_client.py:fetch_contract_list`
deliberately raises `NotImplementedError` rather than guessing at Angel
One's instrument-master format. Real option-chain analytics need contract
discovery (which strikes exist for an expiry) wired up first — for now,
add the contracts you want to track manually via the Django admin
(`OptionContract`), and the snapshot-ingestion task will pull live
quotes for whatever's there. IV specifically is also left as `None` in
`fetch_chain_quotes` — Angel One's standard quote payload doesn't
reliably include it; a separate OptionGreek API call would be needed,
not yet wired in.

- **Risk + Execution + Monitoring hardening (manual section 16: Security
  and Governance), now implemented**:
  - `apps/admin_tools/models.AuditLog` — one central audit trail table
    (not one per app) so "who did what, when" has a single place to
    look, per the manual's "auditable before clever" principle. Nullable
    `actor` (SET_NULL, never CASCADE) so a log entry survives even if
    the user account performing it is later deleted; `actor_label` for
    system/scheduled actions with no human attached
  - `apps/admin_tools/audit.py:log_action()` — the one function every
    other app calls; deliberately never raises, since an audit-write
    failure must never block the real action being logged
  - **RBAC**: `common/permissions.py` (`IsAdminGroup`/`IsTraderOrAdmin`)
    + `setup_rbac_groups` management command. Applied to the two
    genuinely governance-level endpoints — promoting a `StrategyVersion`
    and approving a `DailyReviewNote` — via `get_permissions()`
    branching on the DRF action, so reading stays available to Trader
    role while writing requires Admin. This is real enforcement, not
    just documentation: a Trader-role user's PATCH to promote a
    strategy version now gets rejected, not silently allowed
  - **Audit logging wired into every governance/order action**:
    `rearm_kill_switch`, `apply_review_changes`, strategy version
    promotion, daily review approval, and — most importantly — every
    single order placed or rejected in both `paper_executor.py` and
    `live_executor.py` ("log every order" from the manual, applied to
    real money orders specifically, not just paper ones)
  - **Secrets encryption**: `common/crypto.py` (Fernet-based
    encrypt/decrypt helpers). Honestly scoped — no model currently
    stores a secret in the DB (broker credentials live in `.env`,
    which is already the correct place for them), so this is
    ready-to-use infrastructure for if that ever changes (e.g.
    per-user broker credentials stored in DB), not something applied
    to a field that doesn't need it just to have something to point at

- **Win-probability ML model, now implemented** (additive to the rule
  engine, does not gate BUY/NO_TRADE decisions):
  - `apps/learning/ml_features.py` — the shared, fixed feature set
    (the four existing rule-based scores + one-hot regime + one-hot
    symbol) used identically by training and inference, so the two
    can never silently drift apart on what a column means
  - `apps/learning/ml_train.py:train_win_probability_model` — trains a
    logistic-regression model (deliberately simple/interpretable
    while trade counts are small — see the module docstring for why
    and when to swap in something bigger) from every closed
    `OpenPosition` so far, refuses to train below
    `MIN_TRAINING_SAMPLES` closed trades rather than fitting noise,
    and registers the result in the already-existing `ModelRegistry`
    table (deactivating the previous `win_probability` version)
  - `apps/learning/ml_predict.py:predict_win_probability` — loads the
    active registered model (in-process cached, keyed on registry row
    id) and scores a signal; returns `None` (not a guess) whenever no
    model is trained yet
  - `apps/signals/models.TradingSignal.ml_win_probability` — new
    nullable field, populated by `apps/signals/engine.py`'s
    `_create_signal` helper for every signal (approved, rejected, or
    no-data), the same way every time
  - `apps/learning/tasks.py:retrain_win_probability_model` — Celery
    beat job, weekly (`config/celery.py`), so the model keeps
    reflecting the growing set of your own closed paper trades without
    manual intervention
  - Frontend: `Signals.jsx` shows the ML win-probability per signal;
    `Learning.jsx` shows trained model versions with their
    accuracy/sample-size, so you can see the model is still cold-start
    (empty list) until enough paper trades have closed
  - New deps: `scikit-learn`, `joblib` (added to `requirements.txt`)

- **ML model hardened towards production practice, now implemented**
  (still additive — nothing here lets ML override the rule engine's
  approve/reject decision or apps.risk.engine's hard limits):
  - **Chronological (not random) holdout** in
    `ml_train.py:_fit_and_evaluate` — the evaluation split is always
    the most-recent slice of closed trades by `closed_at`, matching
    the manual's own walk-forward backtesting principle (section 19)
    instead of a random split that would let the model "see the
    future" during evaluation and report an inflated accuracy
  - **Champion/challenger promotion gate**
    (`ml_train.py:_should_promote`) — a freshly-trained model is only
    promoted to `active_flag=True` if its holdout accuracy isn't
    meaningfully worse than the currently-active model's own recorded
    accuracy; a worse challenger still gets saved to `ModelRegistry`
    for visibility but the previous model keeps serving predictions.
    Same "draft, don't auto-apply" governance pattern already used for
    `DailyReviewNote` → `StrategyVersion`
  - **Brier score** (calibration quality) now reported in
    `metrics_json` alongside accuracy/AUC — accuracy alone can look
    fine while the probabilities themselves are miscalibrated, which
    would make confidence-based sizing (next point) misleading
  - **Confidence-based position sizing** —
    `apps/learning/ml_predict.py:ml_confidence_size_multiplier` maps
    `ml_win_probability` to a narrow, hard-bounded multiplier
    (0.7×–1.15×, 1.0× when there's no model yet or probability is
    exactly 50/50) applied in `apps/signals/engine.py` *after*
    `apps.risk.engine` has already approved the trade and computed its
    base size — stacks multiplicatively with the existing regime-based
    multiplier, the same way expiry-day/drawdown-based reductions
    already stack, and can never widen size past
    `settings.RISK_HARD_LIMITS`
  - **ML-specific drift monitoring** —
    `apps/learning/tasks.py:monitor_ml_model_performance`, daily,
    compares the model's live prediction accuracy on real closed
    trades against its own training-time accuracy and logs a
    `DriftEvent` (`drift_type="ml_model_drift"`, its own type,
    separate from the existing strategy-level `check_for_drift`) if
    they diverge — this only ever recommends a retrain, it never
    forces one; the weekly `retrain_win_probability_model` (with its
    own champion/challenger gate) is what actually replaces a degraded
    model

  **On "zero-loss trades"**: no version of this model, however
  production-grade the surrounding pipeline gets, eliminates losing
  trades — market outcomes aren't fully predictable from four
  composite scores (or from anything else). What this pipeline
  actually optimizes for is a *calibrated* probability and disciplined
  sizing around it, with `apps.risk.engine`'s hard stop-loss/kill-switch/
  drawdown limits as the real backstop against large losses. Treat any
  claim otherwise (from this codebase or anywhere else) as a red flag.

  **Honest gap**: this model only has four rule-based scores + regime +
  symbol to learn from — it does not (yet) see raw indicator values,
  option-chain metrics, or news-headline text directly. That's a
  deliberate scope limit for a first pass, not an oversight; once
  there's a real backtest/validation framework (see below) and a few
  hundred closed trades, richer features are the natural next step.
  Also: after `python manage.py makemigrations` for `signals`, run
  `migrate` before this field/model will actually exist in your DB —
  this scaffold was edited without a live DB connection, so the
  migration itself hasn't been generated or applied here.

## What's intentionally NOT implemented

- Backtesting (manual section 19) — the proper foundation for
  validating anything `run_daily_review` suggests, before trusting it
- Intraday equity-curve tracking (for real `max_drawdown`) and a
  paper-vs-live fill comparison (for real `slippage`) in
  `apps/analytics/services.py`
- From the expanded v2 manual (uploaded after Phase 5): Voice Assistant
  (STT/LLM/TTS — a genuinely separate subsystem), News Intelligence
  upgrade (entity/sector/stock impact ranking, beyond the current
  per-headline sentiment), and Security/Governance (RBAC, full audit
  trail, secrets encryption) — none of these have been started

**Important caveat on `apps/execution/live_executor.py` and
`broker_client.place_order`**: this has been written carefully and
mirrors the paper executor's structure, but it has **not been tested
against a real Angel One account**. Before ever setting
`BROKER_MODE=live`:
1. Verify every symbol token in `SYMBOL_TOKENS` against Angel One's
   actual instrument master — the values here are a starting point, not
   verified live tokens.
2. Read `place_order`/`get_order_status` yourself against Angel One's
   current API docs — `smartapi-python`'s exact response shapes have
   changed across versions, and the "handle both shapes" logic in
   `place_order` is a defensive guess, not something exercised against
   a real response.
3. Test with the smallest possible real quantity first.

## pandas-ta removed entirely (Python 3.14 / pandas 3.0 incompatibility)

`pandas-ta==0.3.14b0` was last released around 2021 and never updated
against pandas 3.0 (released 2026) or tested against Python 3.14
(released Oct 2025) at all — patching around its bugs one at a time
(it also had a numpy.NaN import issue) stopped being a reasonable fix.
**Removed entirely.** `apps/market_data/indicators.py` now computes
every indicator (EMA, MACD, RSI, ATR, ADX, Bollinger Band width,
Parabolic SAR) directly with plain pandas/numpy operations — stable
APIs that have been consistent across many pandas major versions.

I didn't just rewrite this and hope — I actually ran the new indicator
math against synthetic candle data in a real Python/pandas/numpy
environment before shipping it: RSI stays correctly bounded 0-100, no
NaNs in the output, and `compute_indicators()`'s full return dict was
verified end-to-end. `apps/signals/engine.py` and
`apps/market_data/regime.py`'s expected dict keys were cross-checked
against the new output too — nothing else needed to change.

## Frontend dependency versions (fixed — see below)

`frontend/package.json` originally pinned dependencies that were
already outdated by the time this was built and had since accumulated
real CVEs (axios pre-1.12.0 has a known DoS vulnerability,
GHSA-4hjh-wcwx-xvwj; vite pre-7.3.2 has path-traversal issues in its
dev server). Bumped to current versions (React 19, react-router-dom 7,
Vite 7, axios 1.19, zustand 5) as of this fix — verified via live
search against npm's registry, not guessed from training data.
`lightweight-charts` was deliberately kept on the 4.x line (not bumped
to 5.x) since v5 changed the chart-series-creation API
(`addCandlestickSeries` → `addSeries(CandlestickSeries, ...)`) and
`PriceChart.jsx` uses the 4.x API; bump that one intentionally, with a
matching code change, if you want v5's improvements.

If `npm install` still flags vulnerabilities after this: delete
`node_modules/` and `package-lock.json` and reinstall fresh (a stale
lockfile pinning old resolved versions is the most common reason
`npm install` shows old versions despite an updated `package.json`),
then run `npm audit fix` as the final, authoritative pass — npm's own
advisory database is more current than any fixed list either of us
writes down.

## Two real bugs fixed (found by tracing the "seeded data but empty dashboard" report)

1. **Chart stayed empty even with real candles in the DB**:
   `HistoricalData.Meta.ordering = ["-timestamp"]` (newest-first — correct
   for the backend's own "give me the latest N" queries), but
   `PriceChart.jsx` fed that array straight into
   `lightweight-charts`' `setData()`, which requires strictly
   **ascending** time order and silently rejects/shows nothing
   otherwise. Fixed by sorting ascending in `PriceChart.jsx` itself
   (not the caller), since the component shouldn't assume callers
   remember to reverse it.
2. **"Most Recent Rejected Trade" could never show anything, ever**:
   `Dashboard.jsx` fetched signals with `{status: "executed"}` (server-side
   filtered), then searched that same already-filtered list for
   `status === "rejected"` — a logical impossibility regardless of what
   was in the database. Fixed by fetching without the status filter.

## Test suite

Every app now has a real `tests.py` (none existed before) covering the
core logic: model constraints, the risk engine's hard-limit checks,
the signal engine's "always logs something" guarantee, options metrics
against fixture data, entity extraction edge cases, RBAC/audit
behavior, and the token-auth endpoint specifically (a regression test
for the earlier 403 bug — the endpoint not existing can't silently
happen again). Run with:
```bash
python manage.py test
```
**Honest caveat**: these were written carefully against the actual
code but have not been run in a live Django+MySQL environment from
here (this sandbox doesn't have Django installed) — run them yourself
and treat any failure as real signal, not a false alarm from my side.

## Why the dashboard shows nothing, and how to fix that

Migrations creating empty tables is necessary but **not sufficient**
for anything to appear on the dashboard — nothing writes a single row
into any of those tables until either:

1. `BROKER_MODE=live` in `.env`, real Angel One credentials, **and** a
   running Celery worker **and** Celery beat process (both separate
   from `python manage.py runserver` — Django alone never executes a
   scheduled task by itself), or
2. Data is seeded manually.

If you've only ever run Django + MySQL + Redis + the frontend, **no
Celery worker/beat has ever run**, so `ingest_watchlist_candles`,
`generate_signals_for_watchlist`, etc. have literally never executed —
that alone fully explains an empty dashboard even with all migrations
correctly applied. To actually run them continuously, you need **two**
worker processes (not one) plus beat — see "Required Windows processes"
below for exactly why, and `scripts/start_platform.ps1` to start all of
this (plus Docker/MySQL/Redis, Django, and the frontend) in one go
instead of opening five terminals by hand.

### Required Windows processes

On Windows, `--pool=solo` is forced automatically (`config/celery.py`),
which means that ONE worker process can only run ONE task at a time --
a slow task (e.g. `ingest_index_chart_candles` retrying through Angel
One rate limits) blocks every other scheduled task queued behind it,
including the option chain's own 5-minute refresh
(`ingest_option_chain_snapshots`), which is why the option chain can
appear to silently stop updating under real rate-limit pressure even
though nothing is actually broken. `config/celery.py` routes that task
(plus `apps.monitoring.tasks.heartbeat_priority_worker`, so this is
directly observable via the health endpoint below) onto its own
`priority` queue for exactly this reason -- you need a **second**
worker process consuming *only* that queue, alongside the default one,
or `ingest_option_chain_snapshots` silently never runs even though
nothing in the logs looks obviously broken:
```bat
celery -A config worker -l info --pool=solo -Q celery
celery -A config worker -l info --pool=solo -Q priority
celery -A config beat -l info
```
If you only ever start the first of these three, everything scheduled
on the default queue still runs fine and nothing errors -- the gap is
silent. Check `GET /api/monitoring/health/` (`celery_priority_worker`)
or the frontend's top-bar feed-status badge to confirm both workers are
actually alive, rather than assuming from the absence of errors.

For the chart/index cards to move **tick-by-tick** (not just every
60s-3min from the Celery beat schedule above), also run the live tick
feed in a 4th terminal (BROKER_MODE=live + real ANGEL_ONE_* creds
required — see `apps/market_data/broker_ws_client.py` for what this
actually opens and why the Celery-beat ingestion alone can't do this):
```bash
python manage.py run_live_feed
```
This process now also runs a dynamic subscription manager
(`apps.options.subscription_manager` + `LiveFeedClient._subscription_refresh_loop`)
that keeps the live option-token subscription scoped to the resolved/
selected expiry and a bounded strike band around ATM
(`OPTIONS_LIVE_STRIKE_RANGE`, default 20 strikes either side) instead of
every synced expiry's every strike -- see that module's own docstring
for the dropped-tick incident this fixes. It re-subscribes automatically
on expiry rollover or an operator's expiry-dropdown change, without
needing a restart.

### Health/status

`GET /api/monitoring/health/` (Trader/Admin auth required) reports
Django/MySQL/Redis connectivity, both Celery worker heartbeats, Celery
Beat, the live Angel One feed's actual connection state and tick
staleness, subscribed option-token count, per-underlying selected
expiry, and the latest detected error category -- never credentials.
The frontend's top-bar badges read this (plus the browser-to-Django
WebSocket status, which is a **separate**, narrower signal -- see
`frontend/src/components/ConnectionStatus.jsx`'s own docstring for why
"the browser socket is connected" must never be read as "the broker
feed is healthy").

**To see something on the dashboard right now**, without needing real
broker credentials yet, use the new `seed_demo_data` management
command (`apps/market_data/management/commands/seed_demo_data.py`):
```bash
python manage.py seed_demo_data
```
This creates starting equity, an active strategy version, ~120
synthetic (clearly `source="seed_demo"`-tagged, NOT real market data)
candles, and then calls the real `generate_signal()` function against
them — so it exercises the actual indicator/regime/risk pipeline
end-to-end, not just fake numbers. Refresh the dashboard after running
it. This is for verifying the plumbing works, not a substitute for
real data — expect the resulting "signal" to be meaningless as an
actual trading idea, since the candles are a random walk.

## Running it locally

**No app ships any migration files as of this pass** (including
`apps/risk`, which previously did) — every `migrations/` folder was
deliberately emptied back to just `__init__.py` at the operator's own
request, for a clean, single, fresh `makemigrations` run against a
brand-new database rather than merging into any previously-generated
migration history. Any older note elsewhere in this README that says
"only apps/risk ships a migration" describes an earlier state of the
project, not the current one — this section is the current, correct
instruction.

**Backend**
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real MySQL credentials, and BROKER_MODE, at minimum
python manage.py makemigrations   # generates every app's migrations fresh, in one pass
python manage.py migrate
python manage.py createsuperuser
python manage.py setup_rbac_groups --add-admin <your-superuser-username>
python manage.py seed_indices
python manage.py runserver
```
You'll also need Redis running locally (`redis-server`, or
`docker compose up -d` from `docker/`, using `docker/.env.example` →
`docker/.env` for real credentials — never hardcode them in
`docker-compose.yml`) for Channels/Celery, and both Celery worker
processes + beat (see "Required Windows processes" above for why two
workers, not one):
```bat
celery -A config worker -l info --pool=solo -Q celery
celery -A config worker -l info --pool=solo -Q priority
celery -A config beat -l info
```
Plus, for real tick-by-tick chart/index-card movement (BROKER_MODE=live only):
```bash
python manage.py run_live_feed
```

**Frontend**
```bash
cd frontend
npm install
npm run dev
```
Then open http://localhost:3000 — the Vite dev server proxies `/api`
and `/ws` to Django on port 8000.

**Or start everything above in one command (Windows)**
```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_platform.ps1
```
Starts Docker/MySQL/Redis, runs `manage.py check` + pending migrations,
then Django/Daphne, both Celery workers, Celery Beat, `run_live_feed`,
and the frontend -- each as its own tracked, logged, size-limited
background process (`logs\<name>.log`, PID in `logs\pids\<name>.pid`).
Re-running it skips any service already running instead of starting a
duplicate. Stop everything it started (and only what it started) with
`scripts\stop_platform.ps1`. See that script's own header comment for
`-SkipDocker`/`-SkipFrontend` and every other detail.

- **JARVIS Voice Assistant, first pass now implemented** (manual
  Chapter 14 -- the piece the manual itself calls the project's
  "heart"):
  - New app `apps/jarvis/`, following the manual's own development
    priority order (14.23): Command Engine (`engine.py`) → Intent
    Detection (`intent.py`) → Navigation Module (`navigation.py`) →
    Response Generator (`responses.py`) → Dashboard Panel
    (`JarvisPanel.jsx`) → Automation Engine (`automation.py`) →
    Context Memory (`memory.py`)
  - `commands.py` — the registry of all ~49 commands from manual
    14.8-14.15 (navigation, market, portfolio, paper trading, AI,
    risk, strategy, automation), each with example phrases
  - `intent.py` — **honest scope note**: this is a rule-based keyword/
    phrase matcher, not a trained NLU model or an LLM call. Transparent
    and needs no API key, but won't understand free-form phrasing
    outside the registered patterns. Swapping it for an LLM-backed
    classifier later is a contained change (callers only depend on
    `detect_intent()`'s return shape)
  - `responses.py` — every handler reads **real data** from the apps
    that already exist (risk.engine, execution, signals, learning,
    analytics, market_data, monitoring, news) — nothing here invents a
    number; a symbol with no candles yet gets "not ingested yet," not
    a fabricated price
  - `automation.py` — manual 14.20's "Market Open Routine" honestly
    scoped: a Django request handler can't start/supervise Celery
    worker/beat processes, so this *checks* the real state of each
    step (feed health, watchlist, recent candles, recent signals) and
    reports exactly what's ready vs. what needs a human to start,
    rather than pretending to run a routine it has no ability to run
  - **Security (manual 14.21), enforced once in `engine.py`, not
    scattered**: `FORBIDDEN_ACTIONS` (kill-switch bypass, live orders,
    editing risk rules) are never even in the command registry —
    there's no code path from JARVIS to those. `RESTRICTED_ACTIONS`
    (reset portfolio, exit all positions, delete history, stop AI
    learning) require an explicit `confirm=True` second call; even
    then, the destructive side effect itself is deliberately *not*
    wired to a one-line voice command (see `engine.py`'s handling of
    the `"RESTRICTED"` sentinel) — those still require the dashboard's
    own explicit action, matching "never execute restricted operations
    without confirmation" (14.25 principle #6) at a stricter level
    than the manual technically required
  - `models.JarvisMemory` / `JarvisCommandHistory` — manual 15.17/
    15.18's tables, built per-user (not a singleton) since v2.0 is
    explicitly multi-user (manual 1.12)
  - **Real event → announcement wiring** (manual 14.16): `signals.py`
    hooks `post_save` on `TradingSignal`, `RiskEvent`, `KillSwitchState`,
    `OpenPosition` (on close), `ModelRegistry`, and `DriftEvent` from
    the apps that already write them, and pushes each as a JARVIS
    announcement over a new `/ws/jarvis/live/` WebSocket group — same
    pattern as `apps.risk.consumers`. One noted honesty caveat:
    detecting "position just closed" relies on `paper_executor.
    close_position()`'s `update_fields=["unrealized_pnl", "closed_at"]`
    call as the signal, since `OpenPosition` has no dedicated
    closed-event field — documented in `signals.py`'s docstring as the
    fragile part or a robust outbox pattern to build later
  - REST: `POST /api/jarvis/command/` (the one endpoint the whole panel
    calls), `GET /api/jarvis/history/`, `GET /api/jarvis/memory/`,
    `GET /api/jarvis/suggested/`
  - Frontend: `JarvisPanel.jsx`, a floating chat widget mounted once in
    `AppShell` (reachable from every page). Voice input uses the
    browser's built-in `SpeechRecognition` API (no external STT
    API/key) with a real, honest degrade — the mic button simply
    doesn't render on browsers that don't support it (Firefox, most
    mobile Safari as of writing), text input still works everywhere.
    Voice *output* uses `SpeechSynthesis`, same no-external-dependency
    approach. `jarvisStore.js` (zustand) holds the conversation and the
    live announcement feed
  - 15 tests in `apps/jarvis/tests.py`, including one that asserts
    every registered command actually has a handler (`test_every_
    registered_command_has_a_handler`) and one that asserts no
    `FORBIDDEN_ACTIONS` entry is ever dispatchable — same "can't
    silently regress" intent as the existing token-auth regression test

  **Honest gaps in this first pass**:
  - Note (superseded): this originally described `apps/jarvis` as the
    one app needing its own `makemigrations jarvis` run, with only
    `apps/risk` shipping a committed migration. As of a later pass, NO
    app ships migrations any more (including `apps/risk`) — a single
    `python manage.py makemigrations && python manage.py migrate` from
    "Running it locally" above covers every app, including this one,
    in one pass.
  - Not tested against a live Django install (same caveat as the rest
    of this scaffold's recent edits — this sandbox has no Django). Run
    `python manage.py test apps.jarvis` yourself first.
  - `market_open_routine`'s readiness report and `intent.py`'s keyword
    matcher have not been tried against real spoken phrasing yet —
    voice-transcription quirks (filler words, symbol mispronunciation)
    will likely need the phrase lists in `commands.py` widened once you
    actually talk to it.
  - AI-side natural conversation (manual 14.22's Version 2.0 features —
    multi-turn free-form dialogue, not just command matching) is
    explicitly out of scope here, matching the manual's own version
    boundary.

## Suggested next step

All 5 phases from the manual's deployment plan now have a working
first pass, wired end-to-end: ingest -> score -> risk-gate -> execute
(paper or live) -> track equity -> daily review -> (approved) new
strategy version -> drift-check that version against its own baseline.

None of it has run against real market data yet, though. The honest
next step isn't more code — it's:
1. Stand up MySQL + Redis (`docker/docker-compose.yml`), run
   migrations, `createsuperuser`.
2. Get real Angel One credentials, verify `SYMBOL_TOKENS`, and run
   `ingest_watchlist_candles` by hand to confirm real candles land in
   `HistoricalData`.
3. Run `generate_signals_for_watchlist` by hand and read the
   `TradingSignal.reason` text critically — does it actually make
   sense, or does a heuristic need fixing before trusting the schedule
   with it?
4. Let it paper-trade for a real stretch of time before ever touching
   `BROKER_MODE=live`, and before ever running `apply_review_changes`
   on a review whose suggestions haven't been sanity-checked by hand.

## Going live with a real Angel One API key — pre-flight checklist

Done as a dedicated review pass once real credentials were about to be
used for the first time (not caught by `py_compile`, since none of
these are syntax errors — they're the kind of bug that runs fine and
silently does the wrong thing):

**Real bugs found and fixed in this pass**:
- `apps/market_data/broker_client.py` and four other files were using
  plain `datetime.now()` / `date.today()` — these follow the host
  machine's own system clock timezone, NOT `settings.TIME_ZONE`
  (Asia/Kolkata). On a server/container defaulting to UTC (very
  common — e.g. a fresh cloud VM), this would silently request the
  **wrong 5.5-hour window** from Angel One's candle API, or the wrong
  calendar day from NSE's history API — no error, just wrong data.
  Fixed to use `django.utils.timezone.localtime()`/`localdate()`
  everywhere except `apps/news/newsapi_client.py`, which correctly
  needs UTC (NewsAPI's own convention) — now explicit about which
  convention each external API expects and why.
- `apps/execution/live_executor.py`: if our own fill-confirmation poll
  times out, the order could still be live at the broker and fill
  later with **zero risk-management/kill-switch visibility** on our
  side. Added `BrokerClient.cancel_order` and wired it into
  `_wait_for_fill`'s timeout path — this reduces, not eliminates, that
  risk (a cancel request can itself race a very-last-moment fill); a
  failed cancel now raises an error that says explicitly to check the
  broker's order book by hand.
- `.env.example` was stale (said "nothing calls out to these yet",
  no longer true) and didn't mention `BROKER_MODE` — the actual master
  switch between paper and live — at all. Rewritten with an accurate,
  step-by-step checklist inline.

**Before setting `BROKER_MODE=live`**:
1. Fill in `ANGEL_ONE_API_KEY`/`CLIENT_ID`/`PASSWORD`/`TOTP_SECRET` in
   `.env` — `TOTP_SECRET` is the TOTP **seed** (base32 string), not a
   live 6-digit code (see `.env.example`'s own note).
2. Confirm `WATCHLIST` symbols all have a real entry in
   `apps/market_data/broker_client.py`'s `SYMBOL_TOKENS` — adding a
   symbol to `WATCHLIST` without a matching token raises `ValueError`
   the first time ingestion runs, not silently.
3. Run `python manage.py shell` and call
   `apps.market_data.broker_client.BrokerClient()._connect()` by hand
   first, in isolation, before turning on any Celery schedule — confirm
   login actually succeeds with your real credentials before anything
   automated depends on it.
4. Only THEN flip `BROKER_MODE=live` and let paper-mode-verified
   signals start reaching `apps.execution.live_executor` — start with
   the smallest possible real quantity, exactly as
   `live_executor.py`'s own module docstring says.
5. **`apps.investing`'s NSE/BSE clients (`fundamentals_client.py`,
   `bse_client.py`) are a COMPLETELY SEPARATE integration from Angel
   One** — your Angel One key does not help those. They scrape NSE's
   and BSE's own public (unofficial) websites for fundamentals/IPO/
   index data and have their own, separate "unverified, least tested"
   caveats (see those files' own docstrings). Don't expect the Angel
   One key to make stock/IPO/sector suggestions "real" — that depends
   entirely on those NSE/BSE endpoints still matching this codebase's
   guessed field names, which has never been checked against a live
   session.

## Later pass: JARVIS proactive notifications, Learning Loop, Options Analytics

- **JARVIS (manual 14.16 "Announces")**: all 8 announcement kinds now
  wired — `market_open`/`market_close` (new scheduled Celery tasks,
  `apps/jarvis/tasks.py`), `trade_executed` now fires on position
  *open* too (previously close-only), and `database_backup_completed`
  (new `backup_database` management command, mysqldump-based). Two
  missing 14.15 automation commands added: `export_trades`,
  `backup_database`. Frontend (`JarvisPanel.jsx`/`jarvisStore.js`):
  announcements now surface without opening the panel — unread badge
  on the FAB, auto-dismissing toasts, and high-priority kinds spoken
  aloud immediately.
- **Learning Loop (manual 11.10/13.8-13.14)**: closed the Reward
  Engine, Decision Confidence bands, Mistake Analysis, and Strategy
  Evaluation gaps — none of these existed before, only the continuous
  ML win-probability did. New `apps/learning/reward.py`,
  `confidence.py`, `mistakes.py`, `ranking.py`, and a new `TradeReview`
  model (manual 13.8 "Experience Memory") populated automatically as
  each position closes (`apps/learning/signals.py`). `run_daily_review`
  now reports average reward score and top mistake tags alongside its
  existing win-rate/expectancy numbers. One honest gap remains: "Late
  Entry" (one of the manual's 7 mistake categories) has no real data
  to detect from in this scaffold — see `mistakes.py`'s docstring.
- **Options Analytics**: no dedicated manual chapter exists for this
  app specifically (only the intro's Problem-7 mention of
  Probability/OI/Greeks/Volume/Risk → Best Strike) — `apps/options/
  models.py`'s own comment references a "section 9 Options
  Intelligence Manual" that isn't part of the uploaded manual PDF, so
  this pass worked from the intro's stated promise plus the existing
  code's own gaps. Added: real Black-Scholes Greeks + a local
  implied-volatility solver (`apps/options/greeks.py`) so IV/Delta/
  Gamma/Theta/Vega no longer depend on Angel One's separate
  (unimplemented) OptionGreek endpoint; a real "Best Strike"
  suggestion (`apps/options/strike_selector.py`) ranking strikes by
  delta/OI/volume/theta — the exact feature the manual's intro
  promises and nothing previously implemented; fixed
  `_latest_underlying_ltp` (was approximating spot price from a
  strike's own value — now reads the real underlying close from
  `HistoricalData`); and a new weekly
  `sync_watchlist_option_contracts` Celery task that automates what
  was previously a fully-manual `sync_option_contracts` command run.

## Later pass: world-class trader features (not manual-specified)

Asked explicitly to go beyond the manual and build what a real trader
needs, independent of whether the manual's own chapters call for it:

- **Real equity curve, max drawdown, Sharpe ratio**: `apps/risk`
  gained a new `EquitySnapshot` model, appended automatically every
  time `AccountEquity` changes (`apps/risk/signals.py`) — this is what
  `apps/analytics/services.py`'s `compute_daily_performance` used to
  leave `max_drawdown` as `None` for (a documented gap: "a true
  intraday max-drawdown needs an equity curve... not just closed-trade
  P&L"). It's real now. New `compute_sharpe_ratio()` (annualized,
  30-day default lookback) exposed via `/api/analytics/sharpe-ratio/`
  and a JARVIS `sharpe_ratio` command.
- **Trailing stop-loss** (`apps/execution/trailing_stop.py`, shared by
  both paper and live executors): opt-in via `TRAILING_STOP_ENABLED`
  in `.env` (off by default). Trails by the position's own original
  risk distance; the stop only ever ratchets up, never down.
- **Price Alerts** (`apps/monitoring.PriceAlert` + `check_price_alerts`
  Celery task, every 2 minutes): a standard trading-platform feature
  that didn't exist at all — trader sets "NIFTY above 25000", checked
  against real `HistoricalData` candles, fires a JARVIS announcement
  when triggered. JARVIS commands `create_price_alert` /
  `list_price_alerts` (natural-language price extraction added to
  `apps/jarvis/intent.py`), plus a `PriceAlertsPanel` on the dashboard.

**Honest gaps in this pass**:
- Note (superseded): this originally said `apps/risk` shipped a
  committed `0002_equitysnapshot.py` migration. As of a later pass, ALL
  migration files (including `apps/risk`'s) were deliberately cleared
  at the operator's request — see "Running it locally" above for the
  current, correct single-pass `makemigrations` instruction.
- Sharpe ratio and max-drawdown only start becoming meaningful once
  `EquitySnapshot` has real history across several actual trading
  days — on a freshly-migrated database both will return `None`/empty
  for a while, correctly (not a bug).
- Trailing stop distance is fixed at "the position's original risk
  distance" — no separate ATR-multiple or percentage-based trailing
  distance is configurable yet; that would be the natural next
  refinement if the fixed-distance behavior doesn't suit a particular
  strategy.
- Price alert direction inference (when the spoken phrase doesn't say
  "above"/"below") falls back to comparing the target price against
  the latest close — reasonable, but genuinely a guess when the
  wording is ambiguous; being explicit in the phrase always overrides it.

## Later pass: long-term stock investing (`apps/investing`) — trader-requested, not manual-specified

Asked explicitly to go beyond intraday index trading and cover
long-term equity investing too: automatic stock suggestions with a
suggested holding period, backed by real fundamentals verified in the
background, a dedicated stock watchlist, and IPO suggestions. No
manual chapter covers any of this — every other app in this codebase
(`market_data`, `signals`, `execution`, `options`) is built around
intraday NIFTY/BANKNIFTY trading on 5-minute candles; this is a
different subsystem on a completely different time scale (quarters and
years, not minutes).

- **`Stock` / `FundamentalSnapshot` / `StockWatchlist` /
  `StockRecommendation` / `IPOListing`** models — an append-only
  fundamentals history per stock (so growth *consistency* across
  recent quarters can be judged, not just the latest quarter's
  numbers), a per-trader watchlist, and generated recommendations kept
  as history rather than overwritten.
- **`apps/investing/fundamentals_client.py`**: real HTTP calls against
  NSE India's public (unofficial) JSON API for quarterly results and
  the IPO calendar — same honesty posture as `apps/options/
  broker_client.py` and `apps/execution/live_executor.py` about their
  own broker integrations: **NOT verified against a live NSE session
  from this sandbox (no network access here)**. Treat it as a
  reviewed-but-unverified starting point; NSE's endpoints are
  undocumented and can drift — check the actual response shape against
  `apps/investing/tasks.py`'s parsing (`_parse_and_store_results`,
  `sync_ipo_calendar`) and adjust field names if needed before relying
  on it.
- **`apps/investing/fundamental_score.py`**: 0-100 composite score
  (revenue/profit growth, growth consistency across up to 4 recent
  quarters, ROE, ROCE, debt-to-equity, promoter holding) → a verdict
  (`strong_hold` / `moderate_hold` / `watch` / `avoid`) → a suggested
  holding period (6-18 months for a moderate candidate, 1.5-5 years
  for a strong one, interpolated by score). Every weight/threshold is
  a documented assumption (no manual formula exists for this), tunable
  in one place. Found and fixed a real interpolation bug during
  development (the band-boundary lookup always resolved to the
  topmost band instead of the adjacent one) — caught by
  `FundamentalScoreTests`, not by inspection.
- Every recommendation's `reasoning` text explicitly states **this is
  a heuristic screen over public ratios, not investment advice** — in
  the data itself, not only in code comments, so the caveat survives
  being read on its own (a dashboard card, a JARVIS response, an API
  consumer that never opens this file).
- Weekly Celery jobs (`refresh_watchlist_fundamentals` →
  `generate_stock_recommendations`, Saturday morning) and a daily
  `sync_ipo_calendar` — fundamentals don't change intraday, so this
  runs nothing like the 2-5 minute cadence the rest of this codebase's
  beat schedule uses.
- 6 new JARVIS commands: `stock_suggestions`, `stock_watchlist`,
  `add_to_stock_watchlist` ("add TCS to my stock watchlist" — the
  symbol is parsed straight out of the utterance, since a long-term
  stock watchlist is open-ended over the whole exchange and can't
  reuse `intent.py`'s small `settings.WATCHLIST`-based symbol list),
  `ipo_suggestions`, plus the price-alert commands from the prior pass.
- New "Stock Investing" dashboard page: watchlist with inline
  score/verdict/hold-period, an automatic-suggestions feed with full
  reasoning text, and an IPO table.

**Honest gaps in this pass**:
- Note (superseded): this originally said `apps/investing` needed its
  own `makemigrations investing` run, separate from `apps/risk`. As of
  a later pass, ALL apps (including `apps/risk`) need a single
  `makemigrations` run with no migration files pre-shipped — see
  "Running it locally" above.
- IPOs have no scoring/ranking logic — a brand-new listing has no
  `FundamentalSnapshot` history to score against, so `ipo_suggestions`
  and the IPO table are a *list* of what's open/upcoming, not a
  ranking; said so explicitly in both the JARVIS response and the UI
  rather than implying a judgment the system can't actually make.
- `fundamentals_client.py`'s NSE endpoint paths and field-name guesses
  (`_parse_and_store_results`) are the single least-verified part of
  this entire codebase — more likely than not to need adjustment
  against real responses before the weekly sync produces anything.
- No valuation factor (P/E, P/B relative to sector or history) is in
  the score at all — a great company at a terrible price still scores
  well here, which the reasoning text says plainly but the number
  itself can't reflect. Sector-relative valuation would be the natural
  next addition if this becomes a serious part of the workflow.

## Later pass: valuation ("is the price high/low/medium?") + technical trend, folded into stock scoring

Asked explicitly to add price/valuation assessment and technical
analysis for stocks, and to base the automatic suggestion on
fundamentals + valuation + technical together — directly closing the
"no valuation factor" gap noted just above.

- **`apps/investing/valuation.py`**: Low/Medium/High price label.
  Primary method is a percentile rank of the CURRENT P/E against the
  stock's OWN historical P/E values (self-referential on purpose — no
  sector-average P/E database exists in this scaffold, and "cheap
  relative to its own history" is a real, meaningful signal on its
  own). Falls back to fixed absolute P/E bands when a stock has fewer
  than 3 P/E data points yet (a newly-watchlisted stock, most
  commonly) — documented as NOT sector-adjusted, since a bank and an
  IT services company have structurally different "normal" P/E ranges.
- **`apps/investing/technical_score.py`**: daily-timeframe trend read
  — SMA50/SMA200 golden-cross/death-cross alignment, plus RSI-14
  (reuses `apps.market_data.indicators._rsi`, the exact same
  Wilder-smoothed formula the intraday engine uses, rather than a
  second implementation that could quietly disagree with it).
  Deliberately much simpler than the intraday indicator engine — a
  months-long hold decision needs "is this in a real uptrend right
  now," not 5-minute-candle noise.
- **New `StockPriceHistory` model** (daily OHLC, separate from
  `apps.market_data.HistoricalData`'s intraday index candles) + a new
  weekly `refresh_watchlist_price_history` Celery task pulling ~400
  days of daily bars per watchlisted stock from NSE's historical-data
  endpoint (same unverified-shape caveat as every other
  `fundamentals_client.py` call).
- **`fundamental_score.py`'s weights rebalanced** to fold both new
  factors in: valuation and technical now each carry real weight (16%
  each) alongside the original fundamentals factors, not bolted on as
  an afterthought — every recommendation's `reasoning` text now states
  the price label and technical trend explicitly, and the JARVIS
  `stock_suggestions` response includes both.
- `StockRecommendation` gained `valuation_label` and `technical_trend`
  fields (blank when the underlying data — P/E or 50+ days of price
  history — isn't available yet, never a fabricated value); the
  Stock Investing dashboard page shows both as color-coded columns.

**Honest gaps in this pass**:
- The valuation fallback (absolute P/E bands) is explicitly NOT
  sector-adjusted — see `valuation.py`'s own module docstring. The
  percentile method doesn't have this problem since it only compares a
  business against its own history, which is why it's the primary
  method whenever there's enough history to use it.
- `technical_score.py` needs 50 real trading days of `StockPriceHistory`
  before it returns anything at all (200 for the full SMA200 golden-
  cross read) — a freshly-added watchlist stock will show no technical
  read for a while even after the weekly sync starts running, same as
  every other "real data takes time to accumulate" gap already noted
  for `EquitySnapshot`/Sharpe ratio in the prior pass.
- `get_historical_prices`'s NSE endpoint/field-name guesses are, like
  the rest of `fundamentals_client.py`, unverified against a live
  session — check the actual response shape before trusting the
  weekly sync to populate anything.

## Later pass: complete historical data import + broker-app-style index dashboard

Asked explicitly for (1) complete previous/historical price data for
all stocks, at real broker-app precision (Upstox/Angel One-grade), and
(2) a dashboard index bar (NIFTY, BANK NIFTY, SENSEX, ...) with live
price movement, clickable to show that index's constituent stock list
-- exactly like a broker app's home screen.

**A real pre-existing bug found and fixed during this pass**:
`apps/investing/models.py`'s `IPOListing` class declaration line had
been accidentally dropped in an earlier edit (this session's own
history, not inherited from the original manual-driven build) --
its fields were silently nested inside `StockPriceHistory` instead of
being their own top-level model. `python -m py_compile` never caught
it (a nested class is syntactically valid Python); it would only have
surfaced the first time IPO code actually ran. Fixed, verified by AST
inspection (`ModelIntegrityTests.test_every_investing_model_is_independently_usable`
now guards against this exact class of mistake happening silently again).

- **`Index` / `IndexConstituent` / `IndexPriceSnapshot`** models +
  `apps.investing.fundamentals_client.get_index_snapshot` (NSE's real
  `equity-stockIndices` endpoint -- one call returns BOTH the index's
  own price and every current member's price). New
  `sync_index_constituents_and_prices` Celery task, every 3 minutes
  during market hours -- the "live-ish" ticker-card data source.
  `seed_indices` management command seeds NIFTY 50, NIFTY BANK, NIFTY
  IT/AUTO/FMCG/PHARMA, and SENSEX.
- **New `IndexTickerBar` dashboard component**: broker-app-style
  ticker cards at the top of the Dashboard page, click a card to see
  that index's constituent stock list (symbol, last price, change %,
  weight).
- **`import_full_stock_history` management command**: real multi-year
  (default 5 years) daily OHLC backfill, in year-sized chunks (NSE's
  historical endpoint appears to cap the date range per request --
  unverified exact limit, see the command's own docstring).
  Deliberately a manual CLI command, not an automated weekly Celery
  task -- backfilling years of history for potentially hundreds of
  index-constituent stocks on an unofficial, unrate-limited-by-us API
  is a meaningfully heavier and more failure-prone load than anything
  else in this codebase's beat schedule, and keeping it a conscious,
  human-run action was judged safer than automating it silently.
- **`_tracked_stocks_queryset()` broadened**: the weekly fundamentals/
  price-history refresh jobs now cover every stock that's EITHER
  watchlisted OR a constituent of any synced Index -- not just a
  trader's personal watchlist. Syncing an index's constituents
  (`sync_index_constituents_and_prices`) automatically pulls its
  members into this tracked set, which is what makes "complete data
  for all stocks" actually mean all of them, not just hand-picked ones.
- Price precision: `StockPriceHistory`/`IndexConstituent`/
  `IndexPriceSnapshot` all store prices as `DecimalField` to 2 decimal
  places (paisa-level) -- this was already true of every price field
  in this codebase (e.g. `OpenPosition.entry_price`), not a new
  addition, but confirmed explicit here since precision was asked for
  directly.

**Honest gaps in this pass**:
- **SENSEX (and any other BSE index) cannot actually be synced** --
  NSE's public API (everything `fundamentals_client.py` uses) has zero
  BSE coverage. The `Index` row exists so the dashboard card renders
  ("not synced yet") rather than being silently absent, but real
  SENSEX data would need a separate BSE India API client this codebase
  doesn't have. `sync_index_constituents_and_prices` explicitly skips
  any `exchange=BSE` index rather than failing on it or faking data.
- NSE's actual per-request date-range limit for historical data was
  never verified against a live session (no network access in this
  sandbox) -- `import_full_stock_history`'s `CHUNK_DAYS=365` is a
  conservative guess; if NSE's real limit is smaller, chunks will come
  back empty/truncated and need adjusting.
- A full 5-year backfill across, say, all 50 NIFTY 50 constituents is
  many dozens of sequential HTTP requests (with a deliberate 1-second
  pause between each) -- this will take real wall-clock time to run,
  by design, not a bug.

## Later pass: BSE support, live index price push, sector-wise analysis + suggestions

Asked explicitly for (1) BSE implementation (closing the SENSEX gap
from the prior pass), (2) broker-website-style "every second" price
movement, (3) tying all of this to AI/ML that continuously observes
price movement/company reaction/growth and verifies sector-wise to
suggest profitable stocks, and (4) a heads-up on what "2.0, public"
would need.

- **`apps/investing/bse_client.py`**: real BSE India public API client
  (SENSEX snapshot + constituents). Same honesty posture as every
  other broker/exchange integration in this codebase -- **the single
  least-verified integration here**, more so than the NSE client: BSE's
  public API is even less commonly documented by independent tools.
  `sync_index_constituents_and_prices` now actually attempts SENSEX
  (previously silently skipped) via a new `_sync_bse_indices` path,
  separate from `_sync_nse_indices` since the two exchanges' response
  shapes differ.
- **"Every second" price movement -- built the real push pipeline,
  and said plainly what it isn't yet**: new `apps/investing/consumers.py`
  + `routing.py` (`ws/investing/index-live/`, wired into
  `config/asgi.py`) + `signals.py` push index/constituent prices to
  the frontend the INSTANT a sync saves them, not on the next REST
  poll — `IndexTickerBar.jsx` now merges live updates via
  `useLiveStore`'s existing WebSocket pattern, with a Live/Reconnecting
  indicator. **What this is NOT**: genuine per-second exchange ticks.
  NSE/BSE's public REST APIs (everything this integration is built on)
  are polled snapshots, not a streaming feed, and hammering them every
  second would almost certainly get this codebase's calling IP
  rate-limited or blocked. New `apps/investing/live_feed.py` documents
  exactly what a real tick feed requires (a broker's streaming
  WebSocket, the same kind `apps.market_data.broker_client` already
  uses for the intraday NIFTY/BANKNIFTY engine) and precisely where to
  plug it in — deliberately not attempted here, since it needs a live,
  funded broker account and a real market session to test, neither
  available in this sandbox.
- **`apps/investing/sector_analysis.py`**: groups the latest
  `StockRecommendation` per stock by `Stock.sector`, surfacing average
  score and the best stock per sector — the "verify sector-wise,
  suggest the profitable stock there" ask. New `/api/investing/
  sector-breakdown/` endpoint, JARVIS `sector_analysis` command, and a
  "Sector Breakdown" panel on the Stock Investing page. Requires at
  least 2 tracked+scored stocks in a sector before naming a "best" one
  — one stock isn't a sector comparison, and the UI/JARVIS both say so
  explicitly rather than presenting a single stock's score as if it
  beat a field of competitors.

**Honest gaps in this pass**:
- SENSEX/BSE data is real but **unverified against a live BSE session**
  — see `bse_client.py`'s own module docstring; this is more likely to
  need endpoint/field-name fixes than the NSE integration is.
- "Company reaction" and per-stock news sentiment (the ML side of
  "కంపెనీస్ ఏ విధంగా రియాక్ట్ అవుతున్నాయి") is **not implemented in
  this pass** — `apps.news` already does FinBERT sentiment scoring, but
  only for the fixed intraday NIFTY/BANKNIFTY watchlist; extending it
  to arbitrary long-term-watchlist stocks (fetching company-specific
  news, not just index-level news) is a real, sizable next step, not
  attempted here to keep this pass's scope honest rather than rushing
  a shallow version of it.
- **On "2.0, public" — a genuine heads-up, not a checklist item to
  silently defer**: giving the public automated stock buy/hold
  suggestions in India is squarely the kind of activity SEBI's
  Investment Adviser (or Research Analyst) Regulations exist to
  govern — this is a real regulatory question a lawyer should answer
  before any public launch, not a software gap this codebase can close
  on its own. Separately, redistributing NSE/BSE data at any real
  public scale is very unlikely to be permitted under either
  exchange's terms for these unofficial public endpoints — a public
  product would need a proper licensed market-data feed, not
  `fundamentals_client.py`/`bse_client.py` as written. Neither of these
  blocks continued personal/private use, which is what was asked for
  right now — they matter specifically at the point of taking this
  public.

## Later pass: AI/ML Market Intelligence — the JARVIS-centered synthesis layer

Asked explicitly for the website's core work to depend on ML/JARVIS
continuously observing all stocks/options/indices/news and explaining
what the market looks like and what's worth buying, because a single
person can't watch everything at once.

- **`apps/jarvis/market_intelligence.py`**: `market_outlook()`
  synthesizes — does NOT re-predict — apps.signals' own latest
  TradingSignal per watchlist symbol (technical score, ML win-
  probability, regime), apps.news' own FinBERT sentiment average,
  apps.options.strike_selector's own best-strike scoring (only when
  the overall bias is directional), and apps.investing.
  fundamental_score's own top StockRecommendation, into one explained
  narrative with an overall Bullish/Bearish/Neutral bias. **Every
  number is traceable to a specific other module a trader can already
  inspect on its own page** — this was a deliberate design choice: a
  single opaque "AI says buy X" score would be far more dangerous than
  an explained rollup of decisions already visible elsewhere in the
  platform. The summary text states this plainly, and states "not
  investment or trading advice," every single time it's generated —
  in the data itself, not only in a docstring, so it survives being
  read on its own (a JARVIS response, a dashboard card).
- New `MarketOutlookSnapshot` model (history, not overwritten) +
  `generate_market_outlook` Celery task, every 15 minutes during
  market hours — announces via JARVIS (manual 14.16-style) only when
  the overall bias actually CHANGES between runs, not on every run
  (announcing "Market bias: Neutral" every 15 minutes all day would
  train a trader to ignore JARVIS announcements entirely).
  New JARVIS `market_outlook` command, `/api/jarvis/market-outlook/`
  + `/history/` endpoints, and a `MarketOutlookPanel` at the top of
  the Dashboard.

**Honest gaps in this pass**:
- The overall-bias combination rule (`_overall_bias` in
  `market_intelligence.py`) is a simple, documented heuristic — a
  majority vote across watchlist symbols' technical bias, adjusted by
  average news sentiment — not a trained model. It inherits whatever
  accuracy apps.signals/apps.news/apps.options/apps.investing actually
  have; it adds no new predictive power of its own, by design (see the
  module's own docstring on why).
- Same as every other JARVIS command reading real data: on a fresh
  install with no signals/news/recommendations generated yet, the
  outlook will correctly read "Neutral, no data" rather than
  fabricating a confident-sounding answer — this is expected, not a bug.

## Later pass: pre-flight review before first real Angel One API key use

Done as a dedicated review pass right before real credentials were
about to be used for the first time — see the "Going live with a real
Angel One API key" checklist above (added in this same pass) for the
full list of real bugs found and fixed (a timezone bug across 5 files
that would have silently fetched the wrong data window on any server
not already set to IST, and an order-fill-timeout safety gap in
`live_executor.py` now mitigated with a real cancel-order attempt) and
the concrete steps to take before flipping `BROKER_MODE=live`.
