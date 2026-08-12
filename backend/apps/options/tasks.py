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

logger = logging.getLogger(__name__)


@shared_task
def sync_watchlist_option_contracts():
    """
    Weekly (config/celery.py beat_schedule): for every underlying in
    settings.WATCHLIST, finds its nearest listed expiry via the real
    instrument master and upserts OptionContract rows for it -- the
    automated equivalent of manually running `python manage.py
    sync_option_contracts --underlying X --expiry <nearest>` for each
    watchlist symbol. Weekly, not daily, because a listed expiry's own
    strikes/tokens almost never change mid-week in practice (see
    OptionContract's docstring) -- what DOES change weekly is which
    expiry counts as "nearest" once the current one expires.

    Only ever ADDS/updates contracts for the nearest expiry -- it never
    deletes stale ones for past expiries, so historical
    OptionChainSnapshot rows stay attached to a real OptionContract for
    analytics/backtesting even after that contract has expired.
    """
    if settings.BROKER_MODE != "live":
        logger.warning("sync_watchlist_option_contracts skipped: BROKER_MODE=%s.", settings.BROKER_MODE)
        return {"skipped": True, "reason": f"BROKER_MODE={settings.BROKER_MODE}"}

    from .broker_client import get_option_chain_client
    from .instrument_master import list_expiries
    from .models import OptionContract

    client = get_option_chain_client()
    results = {}

    for underlying in settings.WATCHLIST:
        underlying = underlying.strip()
        try:
            expiries = list_expiries(underlying, limit=1)
            if not expiries:
                logger.warning("sync_watchlist_option_contracts: no listed expiries for %s.", underlying)
                results[underlying] = {"skipped": True, "reason": "no_expiries_listed"}
                continue

            nearest = expiries[0]
            contracts = client.fetch_contract_list(underlying, nearest)
            created = updated = 0
            for c in contracts:
                _, was_created = OptionContract.objects.update_or_create(
                    underlying=underlying, expiry=nearest,
                    strike=c["strike"], option_type=c["option_type"],
                    defaults={"symbol_token": c["symbol_token"]},
                )
                created += int(was_created)
                updated += int(not was_created)
            results[underlying] = {"expiry": nearest.isoformat(), "created": created, "updated": updated}
        except Exception:
            logger.exception("sync_watchlist_option_contracts failed for %s", underlying)
            results[underlying] = {"error": True}

    return results


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

    contracts = list(OptionContract.objects.all())
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
