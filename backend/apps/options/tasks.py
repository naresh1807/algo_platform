"""
Recurring option-chain snapshot ingestion, plus the weekly contract-
list sync that keeps OptionContract rows current.

ingest_option_chain_snapshots refreshes SNAPSHOTS (OI/IV/volume/price)
for OptionContract rows that ALREADY exist -- it deliberately does not
discover new contracts itself, to keep "what contracts exist" and
"what are their current quotes" as two separate concerns (matching
OptionContract vs. OptionChainSnapshot's own static-vs-time-series
split; see models.py's docstring).

Populating the contract list is sync_watchlist_option_contracts below
(or the equivalent `python manage.py sync_option_contracts` command
for a one-off/manual expiry) -- apps.options.broker_client.
OptionChainClient.fetch_contract_list is a real, working implementation
against Angel One's public instrument master (apps/options/
instrument_master.py), not a stub; earlier revisions of this docstring
said otherwise and that was simply out of date.
"""

import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.market_data.market_hours import is_market_open

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_watchlist_option_contracts(self):
    """
    Daily, before market open (config/celery.py beat_schedule -- changed
    from the original weekly-Monday cadence, see apps.options.
    expiry_service's module docstring for why weekly proved too
    infrequent to reliably catch a mid-week rollover): for every
    underlying in settings.OPTIONS_PIPELINE_UNDERLYINGS, syncs
    OptionContract rows for its nearest settings.OPTIONS_EXPIRY_SYNC_COUNT
    listed expiries via apps.options.contract_sync.sync_all_underlyings
    -- the ONE shared, idempotent, lock-protected sync routine also used
    by `python manage.py sync_option_contracts` and by rollover_expiries/
    options_sync_health_check below, so there is exactly one upsert
    implementation, not three.

    Task name/signature intentionally unchanged from before (still
    zero-arg, still this name) -- config/celery.py's beat_schedule,
    admin/monitoring references, and existing tests all call it by this
    name; only what it does internally changed.

    Retries on unexpected failure (Celery-level, on top of contract_sync's
    own per-underlying try/except) since this is idempotent -- re-running
    it can never double-charge or corrupt state, only re-upsert the same
    real data.
    """
    if settings.BROKER_MODE != "live":
        logger.warning("sync_watchlist_option_contracts skipped: BROKER_MODE=%s.", settings.BROKER_MODE)
        return {"skipped": True, "reason": f"BROKER_MODE={settings.BROKER_MODE}"}

    from .contract_sync import sync_all_underlyings

    try:
        results = sync_all_underlyings()
    except Exception as exc:
        logger.exception("sync_watchlist_option_contracts failed")
        raise self.retry(exc=exc)
    return {underlying: r.as_dict() for underlying, r in results.items()}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def refresh_instrument_master(self):
    """
    Forces a fresh Angel One instrument-master download (apps.options.
    instrument_master.get_instrument_master(force_refresh=True)) --
    scheduled shortly before market open (config/celery.py) so the
    daily sync_watchlist_option_contracts run right after it always
    sees today's listing, including any exchange-holiday-adjusted expiry
    date Angel One published overnight. Retries with the module's own
    internal backoff already covering transient failures within one
    attempt; this Celery-level retry covers the case where even that
    exhausted (e.g. a longer outage) by trying again shortly rather than
    silently leaving the day's sync running against yesterday's cache.
    """
    if settings.BROKER_MODE != "live":
        logger.warning("refresh_instrument_master skipped: BROKER_MODE=%s.", settings.BROKER_MODE)
        return {"skipped": True, "reason": f"BROKER_MODE={settings.BROKER_MODE}"}

    from .instrument_master import InstrumentMasterError, get_instrument_master

    try:
        data = get_instrument_master(force_refresh=True)
    except InstrumentMasterError as exc:
        logger.exception("refresh_instrument_master failed")
        raise self.retry(exc=exc)
    return {"row_count": len(data)}


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def rollover_expiries(self):
    """
    Scheduled shortly after settings.OPTIONS_EXPIRY_CUTOFF_TIME
    (config/celery.py) -- for every underlying, checks apps.options.
    expiry_service.rollover_required() (a cheap, DB-only check) and only
    re-syncs (apps.options.contract_sync.sync_underlying_contracts) the
    ones that actually need it, rather than unconditionally re-syncing
    everything at cutoff time every single day. Also the SELF-HEALING
    entry point: apps.options.tasks.options_sync_health_check calls this
    same task when it detects an underlying has no eligible current
    expiry at all, regardless of time of day -- so if Celery Beat was
    down over the scheduled cutoff, the very next health-check tick
    (every 15 minutes, all day) recovers automatically once Beat is back.
    """
    from .contract_sync import sync_underlying_contracts
    from .expiry_service import rollover_required

    results = {}
    for underlying in settings.OPTIONS_PIPELINE_UNDERLYINGS:
        underlying = underlying.strip()
        if not rollover_required(underlying):
            results[underlying] = {"skipped": True, "reason": "rollover_not_required"}
            continue
        if settings.BROKER_MODE != "live":
            results[underlying] = {"skipped": True, "reason": f"BROKER_MODE={settings.BROKER_MODE}"}
            continue
        try:
            results[underlying] = sync_underlying_contracts(underlying).as_dict()
        except Exception as exc:
            logger.exception("rollover_expiries failed for %s", underlying)
            results[underlying] = {"error": str(exc)}
    return results


@shared_task
def options_sync_health_check():
    """
    Lightweight periodic health check (config/celery.py, every 15
    minutes, all day/every day) -- uses ONLY apps.options.expiry_service.
    rollover_required(), which reads local OptionContract rows (a plain
    DB query), never calling Angel One itself. If any underlying has
    fewer than settings.OPTIONS_EXPIRY_SYNC_COUNT eligible expiries left
    (including the "zero eligible" case -- no valid current expiry at
    all), triggers rollover_expiries asynchronously to actually resync.

    This is the self-heal mechanism the platform was missing entirely:
    previously, if the weekly sync failed or Celery Beat was stopped
    across a rollover, NOTHING would notice or recover until a human
    manually re-ran the sync command -- exactly how the reported
    "still showing the expired contract" bug could persist indefinitely
    once triggered.
    """
    from .expiry_service import rollover_required

    unhealthy = [
        u.strip() for u in settings.OPTIONS_PIPELINE_UNDERLYINGS if rollover_required(u.strip())
    ]
    if unhealthy:
        logger.warning("options_sync_health_check: rollover required for %s -- triggering resync.", unhealthy)
        rollover_expiries.delay()
    return {"unhealthy": unhealthy, "rollover_triggered": bool(unhealthy)}


@shared_task
def ingest_option_chain_snapshots():
    """
    Same BROKER_MODE guard as apps.market_data.tasks.ingest_watchlist_candles
    and apps.execution.tasks -- no-ops with a log line outside live mode.
    """
    if settings.BROKER_MODE != "live":
        logger.warning(
            "ingest_option_chain_snapshots skipped: BROKER_MODE=%s.", settings.BROKER_MODE,
        )
        return {"skipped": True, "reason": f"BROKER_MODE={settings.BROKER_MODE}"}

    from .broker_client import get_option_chain_client
    from .models import OptionChainSnapshot, OptionContract

    # Expired contracts are deliberately never deleted (see
    # sync_watchlist_option_contracts's docstring -- historical snapshots
    # stay attached for backtesting), but they must never be re-quoted
    # here: since that sync task now syncs settings.OPTIONS_EXPIRY_SYNC_COUNT
    # expiries per underlying instead of just one, an unfiltered query
    # here would pull in several times as many contracts every run,
    # multiplying Angel One call volume for expiries nobody can trade
    # any more -- exactly the kind of avoidable load this codebase has
    # hit real AB1021 rate-limit cooldowns from before. is_active (kept
    # correct by apps.options.contract_sync on every sync/rollover run,
    # via apps.options.expiry_service's cutoff-aware eligibility check)
    # replaces the old plain `expiry__gte=today` filter here, which
    # stayed True all day on expiry day itself -- long after that
    # contract's own tradeable life had actually ended at market close.
    contracts = list(OptionContract.objects.filter(is_active=True))
    if not contracts:
        logger.warning(
            "ingest_option_chain_snapshots: no OptionContract rows exist yet -- "
            "sync_watchlist_option_contracts (or `python manage.py sync_option_contracts`) "
            "hasn't populated any yet."
        )
        return {"skipped": True, "reason": "no_contracts_configured"}

    client = get_option_chain_client()
    contract_payload = [
        {"symbol_token": c.symbol_token, "strike": c.strike, "option_type": c.option_type}
        for c in contracts
    ]

    try:
        quotes = client.fetch_chain_quotes(contract_payload)
    except Exception:
        logger.exception("Option chain quote fetch failed")
        return {"error": True}

    now = timezone.now()
    saved = 0
    contracts_by_token = {c.symbol_token: c for c in contracts}

    # Cache one spot-price lookup per underlying per run (not per
    # contract -- an index expiry has 80+ contracts sharing the same
    # underlying) for the local IV solve below.
    from apps.market_data.models import HistoricalData

    spot_by_underlying: dict[str, float | None] = {}

    def _spot_for(underlying: str) -> float | None:
        if underlying not in spot_by_underlying:
            latest = HistoricalData.objects.filter(symbol=underlying).order_by("-timestamp").first()
            spot_by_underlying[underlying] = float(latest.close) if latest else None
        return spot_by_underlying[underlying]

    for quote in quotes:
        contract = contracts_by_token.get(quote["symbol_token"])
        if contract is None or quote.get("ltp") is None:
            continue

        previous = (
            OptionChainSnapshot.objects.filter(contract=contract).order_by("-timestamp").first()
        )
        previous_oi = previous.open_interest if previous else 0

        iv = quote["iv"]
        if iv is None:
            # broker_client.py's own documented gap: the standard quote
            # payload doesn't reliably include IV. Solve it locally from
            # the option's own LTP instead of leaving it permanently
            # None -- see apps.options.greeks module docstring.
            spot = _spot_for(contract.underlying)
            if spot is not None:
                try:
                    from .greeks import DEFAULT_RISK_FREE_RATE, implied_volatility

                    tte_years = (contract.expiry - timezone.localdate()).days / 365.0
                    solved = implied_volatility(
                        float(quote["ltp"]), spot, float(contract.strike),
                        tte_years, DEFAULT_RISK_FREE_RATE, contract.option_type,
                    )
                    iv = round(solved * 100, 2) if solved is not None else None
                except Exception:
                    logger.exception(
                        "Local IV solve failed for %s -- leaving iv unset for this snapshot.", contract,
                    )
                    iv = None

        OptionChainSnapshot.objects.create(
            contract=contract,
            timestamp=now,
            ltp=quote["ltp"],
            open_interest=quote["open_interest"],
            change_in_oi=quote["open_interest"] - previous_oi,
            volume=quote["volume"],
            iv=iv,
            bid=quote["bid"],
            ask=quote["ask"],
        )
        saved += 1

    return {"contracts_snapshotted": saved}


@shared_task
def run_index_direction_strategy(timeframe: str = "5m"):
    """
    Scheduled entry point for apps.options.index_direction_strategy --
    same shape as apps.signals.tasks.generate_signals_for_watchlist
    (market-hours gated inside the task, one symbol's failure doesn't
    stop the others, thin task body with all real logic in the
    strategy module so it stays testable without Celery running).
    Loops index_direction_strategy.INDEX_UNDERLYINGS (NIFTY/BANKNIFTY),
    not settings.WATCHLIST -- this strategy is specifically about those
    two indices' own direction, not every watchlist symbol.
    """
    market_open, reason = is_market_open()
    if not market_open:
        logger.info("run_index_direction_strategy skipped: %s", reason)
        return {"skipped": True, "reason": reason}

    from .index_direction_strategy import INDEX_UNDERLYINGS, evaluate_index_direction_trade

    results = []
    for underlying in INDEX_UNDERLYINGS:
        try:
            signal = evaluate_index_direction_trade(underlying, timeframe)
            results.append({"symbol": underlying, "status": signal.status, "signal_id": signal.pk})
        except Exception:
            logger.exception("evaluate_index_direction_trade failed for %s", underlying)
            results.append({"symbol": underlying, "status": "error"})
    return results
