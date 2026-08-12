"""
Recurring ingestion job: pulls recent candles from the broker (or does
nothing in paper mode -- see the module docstring below) and upserts
them into HistoricalData. This is the job that makes
apps.market_data.indicators / apps.signals.engine have anything to
compute on at all.
"""

import logging
from datetime import datetime

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from celery import shared_task
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone as django_timezone

from apps.monitoring.models import FeedHealthCheck

from .broker_client import BrokerClient, get_broker_client
from .models import HistoricalData

logger = logging.getLogger(__name__)

TIMEFRAME_LOOKBACK_DAYS = {
    "1m": 1,
    "3m": 2,
    "5m": 3,
    "10m": 5,
    "15m": 7,
    "30m": 10,
    "1h": 20,
    "1d": 30,
}


def _lookback_days_for_timeframe(timeframe: str) -> int:
    return TIMEFRAME_LOOKBACK_DAYS.get(timeframe, 5)


@shared_task
def ingest_watchlist_candles(timeframe: str | None = None):
    """
    In BROKER_MODE=paper (the default), this deliberately does nothing
    but log a warning -- there is no broker connection to pull from,
    and silently trying to authenticate against Angel One with empty
    credentials would just produce a confusing BrokerAuthError on every
    run. Switch BROKER_MODE=live (and fill in ANGEL_ONE_* in .env) once
    you're ready to actually pull real data.

    timeframe=None (the default, and what Celery beat calls this with)
    sweeps EVERY timeframe in settings.CHART_TIMEFRAMES for every
    WATCHLIST symbol -- this is what keeps the frontend's timeframe
    dropdown "live" for whichever interval is currently selected,
    without the frontend ever needing to trigger a broker call itself
    (see apps/market_data/views.py: the REST API is read-only).
    Passing an explicit timeframe (e.g. from the shell, or a one-off
    beat entry) restricts the sweep to just that one.
    """
    if settings.BROKER_MODE != "live":
        logger.warning(
            "ingest_watchlist_candles skipped: BROKER_MODE=%s (set to "
            "'live' with real Angel One credentials to actually ingest data).",
            settings.BROKER_MODE,
        )
        return {"skipped": True, "reason": f"BROKER_MODE={settings.BROKER_MODE}"}

    timeframes = [timeframe] if timeframe else settings.CHART_TIMEFRAMES
    client = get_broker_client()
    results = []

    # Small lookback per sweep (5 days) on every timeframe, every 5
    # minutes -- fine even for 1h/1d candles since HistoricalData's
    # unique_together makes re-fetching the same days a harmless no-op
    # (ignore_conflicts in _upsert_candles), and it's what lets a newly
    # selected timeframe already have recent rows instead of showing
    # an empty chart until the next tick happens to cover it.
    for symbol in settings.WATCHLIST:
        for tf in timeframes:
            try:
                candles = client.fetch_recent_candles(
                    symbol,
                    tf,
                    lookback_days=_lookback_days_for_timeframe(tf),
                )
                inserted = _upsert_candles(candles)
                results.append(
                    {
                        "symbol": symbol,
                        "timeframe": tf,
                        "fetched": len(candles),
                        "inserted": inserted,
                    }
                )
            except Exception:
                logger.exception("Candle ingestion failed for %s %s", symbol, tf)
                results.append({"symbol": symbol, "timeframe": tf, "error": True})

    _record_feed_health(client)
    return results


@shared_task
def ingest_index_chart_candles(timeframe: str | None = None):
    """
    Same idea as ingest_watchlist_candles, but for every index
    apps.investing.models.Index tracks (NIFTY 50/BANK/IT/AUTO/FMCG/
    PHARMA, SENSEX -- see seed_indices), not just the two symbols this
    platform actually trades (settings.WATCHLIST). Added 2026-08-08 so
    the dashboard's chart can show real candle history for every index
    card, not only NIFTY/BANKNIFTY.

    Deliberately a SEPARATE task from ingest_watchlist_candles rather
    than just adding these symbols to WATCHLIST -- WATCHLIST drives
    apps.signals (which symbols get BUY/NO_TRADE evaluated),
    apps.risk's position-sizing, and apps.news's per-symbol search
    queries; sector indices like NIFTY AUTO were never meant to be
    traded here, only charted, and folding them into WATCHLIST would
    have silently made the signal engine start evaluating trade setups
    for them too.

    Skips symbols with no configured Angel One token (would only
    happen if an Index row is added to apps.investing without a
    matching apps.market_data.broker_client.SYMBOL_TOKENS entry) rather
    than raising, same "missing config is a skip" convention as
    BROKER_MODE=paper below.
    """
    if settings.BROKER_MODE != "live":
        logger.warning(
            "ingest_index_chart_candles skipped: BROKER_MODE=%s.",
            settings.BROKER_MODE,
        )
        return {"skipped": True, "reason": f"BROKER_MODE={settings.BROKER_MODE}"}

    from apps.investing.models import Index

    from .broker_client import SYMBOL_TOKENS

    timeframes = [timeframe] if timeframe else settings.CHART_TIMEFRAMES
    client = get_broker_client()
    results = []

    for index in Index.objects.all():
        symbol = index.symbol
        if symbol not in SYMBOL_TOKENS:
            results.append(
                {"symbol": symbol, "skipped": True, "reason": "no_token_configured"}
            )
            continue
        for tf in timeframes:
            try:
                candles = client.fetch_recent_candles(
                    symbol,
                    tf,
                    lookback_days=_lookback_days_for_timeframe(tf),
                )
                inserted = _upsert_candles(candles)
                results.append(
                    {
                        "symbol": symbol,
                        "timeframe": tf,
                        "fetched": len(candles),
                        "inserted": inserted,
                    }
                )
            except Exception:
                logger.exception(
                    "Index chart candle ingestion failed for %s %s", symbol, tf
                )
                results.append({"symbol": symbol, "timeframe": tf, "error": True})

    return results


def _upsert_candles(candles: list[dict]) -> int:
    """
    Writes new candles and updates any existing same-timestamp rows.

    The recurring ingestion job fetches a small lookback window every
    poll, including the current in-progress candle. If the broker returns
    the same timestamp again with a new close/volume, we need to update
    the existing row so live charts move instead of waiting for the next
    completed candle.
    """
    if not candles:
        return 0

    for candle in candles:
        timestamp = candle["timestamp"]
        if django_timezone.is_naive(timestamp):
            candle["timestamp"] = django_timezone.make_aware(
                timestamp,
                django_timezone.get_default_timezone(),
            )

    with transaction.atomic():
        symbols = {c["symbol"] for c in candles}
        timeframes = {c["timeframe"] for c in candles}
        timestamps = {c["timestamp"] for c in candles}

        existing_rows = HistoricalData.objects.filter(
            symbol__in=symbols,
            timeframe__in=timeframes,
            timestamp__in=timestamps,
        )
        existing_by_key = {
            (row.symbol, row.timeframe, row.timestamp): row for row in existing_rows
        }

        latest_timestamp_by_key: dict[tuple[str, str], datetime] = {}
        for candle in candles:
            key = (candle["symbol"], candle["timeframe"])
            current_latest = latest_timestamp_by_key.get(key)
            if current_latest is None or candle["timestamp"] > current_latest:
                latest_timestamp_by_key[key] = candle["timestamp"]

        to_create = []
        to_update = []
        for c in candles:
            key = (c["symbol"], c["timeframe"], c["timestamp"])
            existing = existing_by_key.get(key)
            if existing is None:
                to_create.append(HistoricalData(**c))
                continue

            changed = False
            for field in ("open", "high", "low", "close", "volume", "source"):
                if getattr(existing, field) != c[field]:
                    setattr(existing, field, c[field])
                    changed = True
            if changed:
                to_update.append(existing)

        HistoricalData.objects.bulk_create(
            to_create,
            ignore_conflicts=True,
        )

        latest_changed: dict[tuple[str, str], dict] = {}
        for candle in candles:
            key = (candle["symbol"], candle["timeframe"])
            if candle["timestamp"] != latest_timestamp_by_key[key]:
                continue
            existing = existing_by_key.get(
                (candle["symbol"], candle["timeframe"], candle["timestamp"])
            )
            if existing is None or any(
                getattr(existing, field) != candle[field]
                for field in ("open", "high", "low", "close", "volume", "source")
            ):
                latest_changed[key] = candle

        channel_layer = get_channel_layer()
        if channel_layer is not None:
            for candle in latest_changed.values():
                async_to_sync(channel_layer.group_send)(
                    "market_data_live",
                    {
                        "type": "candle_update",
                        "data": {
                            "symbol": candle["symbol"],
                            "timeframe": candle["timeframe"],
                            "timestamp": candle["timestamp"].isoformat(),
                            "open": float(candle["open"]),
                            "high": float(candle["high"]),
                            "low": float(candle["low"]),
                            "close": float(candle["close"]),
                            "volume": candle["volume"],
                        },
                    },
                )

        for row in to_update:
            row.save(update_fields=["open", "high", "low", "close", "volume", "source"])

        return len(to_create)


def _record_feed_health(client: BrokerClient) -> None:
    health = client.check_feed_health()
    FeedHealthCheck.objects.create(
        source="angel_one",
        is_healthy=health["is_healthy"],
        latency_ms=health["latency_ms"],
        detail=health["detail"],
    )
    if not health["is_healthy"]:
        logger.error("Broker feed unhealthy: %s", health["detail"])
