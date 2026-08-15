"""
Recurring jobs for apps.investing:
  - refresh_watchlist_fundamentals / refresh_watchlist_price_history:
    pull real quarterly results / daily OHLC bars from
    apps.investing.fundamentals_client for every stock this app is
    tracking -- "tracking" means on ANY trader's StockWatchlist OR a
    constituent of a synced Index (see sync_index_constituents_and_prices
    below), not only a personal watchlist. This is what makes "give me
    complete previous data for all stocks" actually cover all stocks,
    not just the ones a trader happened to add by hand.
  - generate_stock_recommendations: re-scores every stock that has at
    least one FundamentalSnapshot (apps.investing.fundamental_score),
    writing a fresh StockRecommendation row -- this is what makes the
    "automatic suggestions" the trader asked for actually automatic,
    rather than only computed on demand when someone opens the page.
  - sync_ipo_calendar: pulls the current IPO list from the same client.
  - sync_index_constituents_and_prices: the broker-app-style dashboard
    the trader asked for -- NIFTY/BANK NIFTY/etc ticker cards with live
    price movement, click-through to the constituent stock list. Price
    comes from Angel One; membership comes from a static per-index CSV
    NSE publishes (apps.investing.fundamentals_client.NSEDataClient.
    get_index_constituents_csv) -- this ALSO seeds/updates the Stock
    rows for every member, which is what feeds them into the "tracked"
    set the fundamentals/price-history jobs above pick up automatically.

Fundamentals and daily price history change at most once a day (most
of the time, once a quarter) -- these run weekly, not on the 2-5
minute cadence the rest of this codebase's Celery beat schedule uses
for intraday data. Index prices are the one thing here that's
genuinely "live-ish" and run far more often -- see config/celery.py
for the actual schedule of each.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from celery import shared_task

logger = logging.getLogger(__name__)


def _tracked_stocks_queryset():
    """
    Every Stock this app should keep fundamentals/price-history current
    for: on a trader's watchlist, OR a member of any synced Index.
    Shared by refresh_watchlist_fundamentals and
    refresh_watchlist_price_history so "which stocks count as tracked"
    can't drift between the two.
    """
    from django.db.models import Q

    from .models import Stock

    return Stock.objects.filter(Q(watchers__isnull=False) | Q(index_memberships__isnull=False)).distinct()


def _to_decimal(value) -> Decimal | None:
    if value in (None, "", "-"):
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


def _to_float(value) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _parse_and_store_results(stock, raw_results: list[dict]) -> int:
    """
    Maps NSE's raw corporate-results rows onto FundamentalSnapshot.
    Field names below are apps.investing.fundamentals_client's own
    documented best-effort guess at NSE's response shape (see that
    module's honesty caveat) -- adjust the .get() keys here first if
    real data comes back with different field names than expected.
    """
    from .models import FundamentalSnapshot

    created = 0
    for row in raw_results:
        period_end_raw = row.get("toDate") or row.get("period_end")
        if not period_end_raw:
            continue
        try:
            period_end = datetime.strptime(period_end_raw, "%d-%b-%Y").date()
        except (ValueError, TypeError):
            logger.warning("Unparseable period_end %r for %s -- skipping row.", period_end_raw, stock.symbol)
            continue

        _, was_created = FundamentalSnapshot.objects.update_or_create(
            stock=stock, period_end=period_end, reporting_type="quarterly",
            defaults={
                "revenue": _to_decimal(row.get("income") or row.get("revenue")),
                "net_profit": _to_decimal(row.get("profitLoss") or row.get("net_profit")),
                "eps": _to_decimal(row.get("eps")),
                "revenue_growth_yoy": _to_float(row.get("revenueGrowthYoy")),
                "profit_growth_yoy": _to_float(row.get("profitGrowthYoy")),
                "roe": _to_float(row.get("roe")),
                "roce": _to_float(row.get("roce")),
                "debt_to_equity": _to_float(row.get("debtToEquity")),
                "promoter_holding_pct": _to_float(row.get("promoterHolding")),
                "pe_ratio": _to_float(row.get("pe")),
                "source": "nse_corporate_results",
            },
        )
        created += int(was_created)
    return created


@shared_task
def refresh_watchlist_fundamentals():
    from .fundamentals_client import NSEDataClient

    stocks = _tracked_stocks_queryset()
    if not stocks:
        logger.info("refresh_watchlist_fundamentals: no tracked stock yet -- nothing to fetch.")
        return {"stocks_checked": 0}

    client = NSEDataClient()
    results = {}
    for stock in stocks:
        try:
            raw = client.get_financial_results(stock.symbol, period="Quarterly")
            created = _parse_and_store_results(stock, raw)
            results[stock.symbol] = {"snapshots_created": created}
        except Exception:
            logger.exception("refresh_watchlist_fundamentals failed for %s", stock.symbol)
            results[stock.symbol] = {"error": True}

    return results


@shared_task
def refresh_watchlist_price_history():
    """
    Daily OHLC bars for every TRACKED stock (see _tracked_stocks_queryset
    -- watchlisted OR an index constituent) -- feeds
    apps.investing.technical_score (needs 50-200 days of history) and
    apps.investing.valuation's 52-week-range context. Fetches ~400
    calendar days (comfortably covers 200 trading days even with
    weekends/holidays) on every run rather than only the delta since
    last sync -- simpler, and update_or_create makes re-fetching
    already-stored days a cheap no-op, not a duplicate. For real
    multi-year backfill (more than this ~400-day rolling window),
    see the `import_full_stock_history` management command instead --
    deliberately NOT automated on a schedule, see that command's own
    docstring for why.
    """
    from datetime import timedelta

    from django.utils import timezone

    from .fundamentals_client import NSEDataClient
    from .models import StockPriceHistory

    stocks = _tracked_stocks_queryset()
    if not stocks:
        logger.info("refresh_watchlist_price_history: no tracked stock yet -- nothing to fetch.")
        return {"stocks_checked": 0}

    client = NSEDataClient()
    to_date = timezone.localdate()
    from_date = to_date - timedelta(days=400)

    results = {}
    for stock in stocks:
        try:
            raw = client.get_historical_prices(stock.symbol, from_date, to_date)
            created = 0
            for row in raw:
                date_raw = row.get("CH_TIMESTAMP") or row.get("date")
                if not date_raw:
                    continue
                try:
                    bar_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                _, was_created = StockPriceHistory.objects.update_or_create(
                    stock=stock, date=bar_date,
                    defaults={
                        "open": _to_decimal(row.get("CH_OPENING_PRICE") or row.get("open")),
                        "high": _to_decimal(row.get("CH_TRADE_HIGH_PRICE") or row.get("high")),
                        "low": _to_decimal(row.get("CH_TRADE_LOW_PRICE") or row.get("low")),
                        "close": _to_decimal(row.get("CH_CLOSING_PRICE") or row.get("close")),
                        "volume": row.get("CH_TOT_TRADED_QTY") or row.get("volume"),
                        "source": "nse_historical",
                    },
                )
                created += int(was_created)
            results[stock.symbol] = {"bars_created": created}
        except Exception:
            logger.exception("refresh_watchlist_price_history failed for %s", stock.symbol)
            results[stock.symbol] = {"error": True}

    return results


@shared_task
def generate_stock_recommendations():
    from .fundamental_score import score_stock
    from .models import Stock, StockRecommendation

    generated = 0
    for stock in Stock.objects.filter(fundamentals__isnull=False).distinct():
        result = score_stock(stock)
        if result is None:
            continue
        StockRecommendation.objects.create(
            stock=stock,
            based_on=result["based_on_snapshot"],
            fundamental_score=result["score"],
            verdict=result["verdict"],
            suggested_hold_months=result["suggested_hold_months"],
            valuation_label=result["valuation_label"],
            technical_trend=result["technical_trend"],
            reasoning=result["reasoning"],
        )
        generated += 1

        if result["verdict"] in ("strong_hold", "moderate_hold"):
            try:
                from apps.jarvis.notify import announce
                announce(
                    "stock_recommendation",
                    f"{stock.symbol}: {result['reasoning'].split('.')[0]}. "
                    f"Suggested hold: ~{result['suggested_hold_months']} months.",
                    symbol=stock.symbol,
                )
            except Exception:
                logger.exception("Failed to announce stock recommendation for %s", stock.symbol)

    return {"recommendations_generated": generated}


@shared_task
def sync_ipo_calendar():
    from .fundamentals_client import NSEDataClient
    from .models import IPOListing

    client = NSEDataClient()
    raw = client.get_ipo_list()
    if not raw:
        logger.info("sync_ipo_calendar: no IPO data returned (or endpoint shape has drifted -- see fundamentals_client's caveat).")
        return {"ipos_synced": 0}

    synced = 0
    for row in raw:
        company_name = row.get("companyName") or row.get("company_name")
        if not company_name:
            continue

        def parse_date(key):
            raw_date = row.get(key)
            if not raw_date:
                return None
            try:
                return datetime.strptime(raw_date, "%d-%b-%Y").date()
            except (ValueError, TypeError):
                return None

        IPOListing.objects.update_or_create(
            company_name=company_name,
            defaults={
                "symbol": row.get("symbol", ""),
                "exchange": row.get("series", ""),
                "open_date": parse_date("issueStartDate"),
                "close_date": parse_date("issueEndDate"),
                "price_band_low": _to_decimal(row.get("issuePriceLow")),
                "price_band_high": _to_decimal(row.get("issuePriceHigh")),
                "issue_size_crores": _to_decimal(row.get("issueSize")),
                "status": row.get("status", "upcoming").lower(),
                "source": "nse_ipo_list",
            },
        )
        synced += 1

    return {"ipos_synced": synced}


@shared_task
def sync_index_constituents_and_prices():
    """
    The broker-app-style dashboard the trader asked for: NIFTY/BANK
    NIFTY/SENSEX/etc ticker cards with live-ish price movement,
    click-through to the constituent stock list. Runs every few
    minutes during market hours (config/celery.py) -- the closest
    thing to "live" a REST-poll-based integration can offer; see
    apps/investing/live_feed.py for what genuine per-second ticks
    would actually require and why this task can't provide that on
    its own.

    PRICE comes from Angel One (_sync_index_prices_via_angelone below) --
    added 2026-08-08 after nseindia.com's Akamai bot protection was
    confirmed to block this platform's NSE scraper outright (plain
    `requests` calls get a blocked/404 challenge page, even with a real
    browser User-Agent and cookie bootstrap; see
    apps.investing.fundamentals_client's module docstring, which already
    flagged this integration as unverified). Angel One's instrument
    master already carries real, working tokens for every index this
    platform tracks, and this platform already has a working Angel One
    session (apps.market_data.broker_client) -- reusing it for index
    price sidesteps the Akamai problem entirely rather than fighting it.

    CONSTITUENT STOCK LISTS still come from _sync_nse_indices/
    _sync_bse_indices below (kept, best-effort) -- Angel One's
    instrument master has no concept of "which stocks belong to NIFTY
    AUTO," that's NSE/BSE-published index-membership data with no
    broker-API equivalent. Those two functions still attempt the
    Akamai-blocked scrape (harmless when it fails, exactly the same
    graceful-empty-result handling as before) since it's the only
    source for this platform's constituent data; a static/hardcoded
    constituent list is a reasonable future fallback if this needs to
    work reliably, not attempted here.
    """
    from .models import Index

    nse_indices = Index.objects.filter(exchange=Index.Exchange.NSE)
    bse_indices = Index.objects.filter(exchange=Index.Exchange.BSE)
    all_indices = list(nse_indices) + list(bse_indices)

    if not all_indices:
        logger.info("sync_index_constituents_and_prices: no Index rows exist yet -- run the seed_indices management command first.")
        return {"indices_checked": 0}

    results = {}
    price_results = _sync_index_prices_via_angelone(all_indices)
    results.update(_sync_nse_indices(nse_indices))
    results.update(_sync_bse_indices(bse_indices))
    for name, price_result in price_results.items():
        results.setdefault(name, {}).update(price=price_result)
    return results


def _sync_index_prices_via_angelone(indices) -> dict:
    """
    Reliable index LTP source (see sync_index_constituents_and_prices's
    own docstring for why this exists instead of relying on the
    Akamai-blocked NSE/BSE scrapers for price). No-ops with a clear
    skip reason in BROKER_MODE=paper, same convention as every other
    Angel-One-dependent task in this codebase (apps.market_data.tasks,
    apps.options.tasks) -- fetching a real quote needs a real broker
    session, which paper mode deliberately never opens.
    """
    from django.conf import settings
    from django.utils import timezone

    if settings.BROKER_MODE != "live":
        logger.info("_sync_index_prices_via_angelone skipped: BROKER_MODE=%s.", settings.BROKER_MODE)
        return {index.name: {"skipped": True, "reason": f"BROKER_MODE={settings.BROKER_MODE}"} for index in indices}

    from apps.market_data.broker_client import get_broker_client
    from apps.options.instrument_master import find_index_token

    from .models import IndexPriceSnapshot

    client = get_broker_client()
    results = {}

    for index in indices:
        token_info = find_index_token(index.name, index.exchange)
        if token_info is None:
            logger.warning(
                "_sync_index_prices_via_angelone: no Angel One instrument-master token found for %r on %s.",
                index.name, index.exchange,
            )
            results[index.name] = {"error": True, "reason": "no_token_found"}
            continue

        try:
            quote = client.get_quote_by_token(index.exchange, token_info["tradingsymbol"], token_info["token"])
        except Exception:
            logger.exception("_sync_index_prices_via_angelone: quote fetch failed for %s.", index.name)
            results[index.name] = {"error": True}
            continue

        # round() BEFORE converting to Decimal/saving -- raw float
        # subtraction (e.g. 24570.65 - 24636.0) routinely produces
        # binary floating-point artifacts like -65.34999999999854
        # instead of -65.35. IndexPriceSnapshot.change's DecimalField
        # (decimal_places=2) rounds this away on DB storage/retrieval,
        # but apps.investing.signals' post_save broadcast sends
        # str(instance.change) straight from the in-memory Python
        # object *before* that DB round-trip -- without rounding here
        # first, the live WebSocket-pushed dashboard value would show
        # the ugly unrounded float even though a fresh REST API fetch
        # (which goes through DRF's Decimal serializer, which does
        # quantize) would look clean. Round at the source once, instead
        # of relying on two different downstream paths to each do it
        # consistently.
        change = round(quote["ltp"] - quote["close"], 2)
        change_pct = round(change / quote["close"] * 100, 4) if quote["close"] else None
        IndexPriceSnapshot.objects.create(
            index=index, timestamp=timezone.now(),
            ltp=_to_decimal(quote["ltp"]), change=_to_decimal(change),
            change_pct=change_pct,
        )
        results[index.name] = {"synced": True, "ltp": quote["ltp"]}

    return results


def _sync_nse_indices(indices) -> dict:
    """
    Membership only (which stocks belong to this index) -- via
    NSEDataClient.get_index_constituents_csv's static-file source, see
    that method's own docstring for why (the single-call endpoint that
    used to also carry live member prices/weights, /api/equity-
    stockIndices, is confirmed dead as of 2026-08-15). last_price/
    change_pct/weight_pct are deliberately left untouched here (not
    zeroed out) -- this sync doesn't have that data at all, and index
    PRICE already comes from _sync_index_prices_via_angelone; nothing
    in this codebase currently populates per-constituent price/weight,
    so there's nothing to preserve today, but a future source for that
    shouldn't get silently clobbered by this one running every 3
    minutes.
    """
    from .fundamentals_client import NSEDataClient
    from .models import IndexConstituent, Stock

    if not indices:
        return {}

    client = NSEDataClient()
    results = {}

    for index in indices:
        rows = client.get_index_constituents_csv(index.name)
        if not rows:
            logger.warning("sync_index_constituents_and_prices: empty/no response for %s.", index.name)
            results[index.name] = {"error": True}
            continue

        members_synced = 0
        for row in rows:
            symbol = row.get("symbol")
            if not symbol:
                continue

            company_name = row.get("company_name") or symbol
            stock, _ = Stock.objects.get_or_create(symbol=symbol, defaults={"name": company_name})
            IndexConstituent.objects.get_or_create(index=index, stock=stock)
            members_synced += 1

        results[index.name] = {"members_synced": members_synced}

    return results


def _sync_bse_indices(indices) -> dict:
    """
    Currently only meaningfully handles SENSEX -- BSEDataClient's own
    two calls (get_sensex_snapshot/get_sensex_constituents) are
    SENSEX-specific, matching what BSE's public API actually appears
    to expose (see bse_client.py's own caveat about this being the
    least-verified integration in this codebase). Any other BSE Index
    row seeded (there are none by default -- see seed_indices) would
    need its own client method added first; this silently skips
    anything that isn't SENSEX rather than guessing at a generic
    BSE-index-by-name call that may not exist.
    """
    from .models import IndexConstituent, IndexPriceSnapshot, Stock

    if not indices:
        return {}

    from django.utils import timezone

    from .bse_client import BSEDataClient

    client = BSEDataClient()
    results = {}

    for index in indices:
        if index.name != "SENSEX":
            logger.info("_sync_bse_indices: no client method for BSE index %r yet -- skipping.", index.name)
            results[index.name] = {"skipped": True, "reason": "no_bse_client_support_for_this_index"}
            continue

        snapshot = client.get_sensex_snapshot()
        if snapshot:
            ltp = _to_decimal(snapshot.get("CurrValue") or snapshot.get("lastPrice"))
            if ltp is not None:
                IndexPriceSnapshot.objects.create(
                    index=index, timestamp=timezone.now(),
                    ltp=ltp, change=_to_decimal(snapshot.get("Chg")), change_pct=_to_float(snapshot.get("PcChg")),
                )

        constituents = client.get_sensex_constituents()
        members_synced = 0
        for row in constituents:
            symbol = row.get("scrip_cd") or row.get("Scrip_Cd") or row.get("symbol")
            company_name = row.get("scrip_name") or row.get("Scrip_Name") or symbol
            if not symbol:
                continue

            stock, _ = Stock.objects.get_or_create(
                symbol=str(symbol), defaults={"name": company_name, "exchange": Stock.Exchange.BSE},
            )
            IndexConstituent.objects.update_or_create(
                index=index, stock=stock,
                defaults={
                    "last_price": _to_decimal(row.get("ltp") or row.get("LTP")),
                    "change_pct": _to_float(row.get("perchange") or row.get("PerChange")),
                },
            )
            members_synced += 1

        results[index.name] = {"members_synced": members_synced}

    return results
