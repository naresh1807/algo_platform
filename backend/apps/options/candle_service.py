"""
Resolves an exact OptionContract server-side (never trusting a
frontend-supplied token/tradingsymbol -- see resolve_contract's own
docstring) and returns its OHLCV candle history, fetching from Angel
One only when the DB doesn't already have a fresh-enough copy.

MySQL IS the cache the "cache historical responses" requirement asks
for: apps.options.models.OptionCandle rows are unique per
(contract, timeframe, timestamp), so once a range has been fetched and
stored it never needs a second broker round-trip for that same range --
exactly the same "no CACHES setting needed" convention
apps.options.contract_sync/instrument_master already established for
this codebase (see this module's docstring history for why: adding a
whole new cache backend for something the database already does
correctly would be unrelated scope creep). A short django.core.cache
mutex (same backend EvaluateNowView already uses) only guards against
two near-simultaneous requests for the SAME contract+timeframe both
triggering a broker call at once -- not a data cache, a stampede guard.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import transaction
from django.db.utils import IntegrityError
from django.utils import timezone as django_timezone

from apps.market_data.broker_client import MAX_LOOKBACK_DAYS, TIMEFRAME_TO_ANGEL_INTERVAL
from apps.market_data.indicators import _TIMEFRAME_DURATION

from .models import OptionCandle, OptionContract

logger = logging.getLogger(__name__)

# Angel One option contracts are always NFO -- OptionContract itself
# has no `exchange` column (every row IS an NFO option by construction,
# see apps.options.instrument_master/contract_sync), so this is the one
# place that constant is named rather than a magic string repeated in
# the broker call, the API response, and tests.
OPTION_EXCHANGE = "NFO"

TIMEFRAME_CHOICES = frozenset(TIMEFRAME_TO_ANGEL_INTERVAL.keys())

# Smaller default than MAX_LOOKBACK_DAYS -- a chart opening for the
# first time doesn't need years of 1-minute history, just enough to
# render a useful chart; a caller that wants more can pass an explicit
# from/to range.
DEFAULT_LOOKBACK_DAYS = {
    "1m": 5, "3m": 5, "5m": 5, "10m": 10, "15m": 10, "30m": 10, "1h": 30, "1d": 200,
}

FETCH_LOCK_TIMEOUT_SECONDS = 30


class ContractResolutionError(Exception):
    """Raised when the requested contract identity doesn't resolve to a real, synced OptionContract row."""


def resolve_contract(
    *, contract_id=None, token=None, underlying=None, expiry=None, strike=None, option_type=None,
) -> OptionContract:
    """
    The ONE place a contract identity (from a frontend click, or from a
    WS subscription) turns into a real OptionContract row. The frontend
    never constructs a symbol_token/tradingsymbol itself -- it only ever
    sends what it already legitimately knows (a contract id it was
    handed earlier, or the underlying/expiry/strike/option_type a chain
    row click provides), and this function is what turns that into the
    broker-facing identity (token, tradingsymbol). Resolution against
    OptionContract also IS the "confirm this is an allowed NFO
    instrument" check the caller needs -- that table is populated only
    by apps.options.contract_sync from Angel One's real instrument
    master, so a row existing here already means "a real, listed NFO
    option," nothing further to validate.
    """
    if contract_id is not None:
        try:
            return OptionContract.objects.get(id=contract_id)
        except (OptionContract.DoesNotExist, ValueError, TypeError):
            raise ContractResolutionError(f"No option contract with id={contract_id!r}.")

    if token is not None:
        try:
            return OptionContract.objects.get(symbol_token=token)
        except OptionContract.DoesNotExist:
            raise ContractResolutionError(f"No option contract with token={token!r}.")

    if underlying and expiry is not None and strike is not None and option_type:
        try:
            return OptionContract.objects.get(
                underlying=underlying, expiry=expiry, strike=strike, option_type=option_type,
            )
        except (OptionContract.DoesNotExist, ValueError, TypeError, InvalidOperation, ArithmeticError):
            raise ContractResolutionError(
                f"No option contract for {underlying} {strike} {option_type} expiring {expiry}."
            )
        except Exception as exc:  # noqa: BLE001 -- a malformed strike (e.g. non-numeric) raises Django's own ValidationError/DecimalException variants
            raise ContractResolutionError(f"Could not resolve contract: {exc}")

    raise ContractResolutionError(
        "Provide either contract, token, or underlying+expiry+strike+option_type to resolve a contract."
    )


def _validate_ohlc(row: dict) -> bool:
    """
    Real, sane OHLC before it's ever saved or returned -- Angel One has
    been observed to occasionally include a malformed row in candle
    history responses (a zero/negative price, or a high/low that
    doesn't bound open/close), and a candle chart rendering that
    literally would show an impossible bar. Rejected rows are dropped
    (logged), never coerced into something fake.
    """
    try:
        o, h, l, c = (Decimal(str(row[k])) for k in ("open", "high", "low", "close"))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        return False
    if h < l:
        return False
    if h < o or h < c or l > o or l > c:
        return False
    return True


def _stored_candles(contract: OptionContract, timeframe: str, from_dt, to_dt) -> list[OptionCandle]:
    qs = OptionCandle.objects.filter(contract=contract, timeframe=timeframe)
    if from_dt is not None:
        qs = qs.filter(timestamp__gte=from_dt)
    if to_dt is not None:
        qs = qs.filter(timestamp__lte=to_dt)
    return list(qs.order_by("timestamp"))


def _is_fresh_enough(rows: list[OptionCandle], timeframe: str, to_dt) -> bool:
    if not rows:
        return False
    duration = _TIMEFRAME_DURATION.get(timeframe)
    if duration is None:
        return True
    newest = rows[-1].timestamp
    reference = to_dt or django_timezone.now()
    # The stored copy is "fresh enough" once its newest bar is within
    # two bar-durations of the requested/current time -- one duration
    # for the bar's own still-forming period (matches
    # apps.market_data.indicators._drop_unclosed_last_bar's own
    # reasoning for what "not closed yet" means), a second so a request
    # arriving just after a bucket rollover (before the live feed's own
    # aggregator has persisted the brand new bar yet) doesn't
    # unnecessarily trigger a broker call for one bar's worth of gap.
    return (reference - newest) < (duration * 2)


def _row_dict(row: OptionCandle) -> dict:
    return {
        "time": row.timestamp.isoformat(),
        "open": float(row.open), "high": float(row.high), "low": float(row.low), "close": float(row.close),
        "volume": row.volume,
    }


def get_option_candles(
    contract: OptionContract, timeframe: str, from_dt: datetime | None = None, to_dt: datetime | None = None,
) -> list[dict]:
    """
    Returns ascending, deduplicated OHLCV dicts for exactly one
    contract+timeframe. Sorting/deduplication both fall directly out of
    the DB query (Meta.ordering + unique_together), not a Python pass
    over the results -- there is only ever one row per
    (contract, timeframe, timestamp) to begin with.

    Serves straight from OptionCandle when it's already fresh enough
    (no broker call at all). Otherwise fetches from Angel One (this
    contract's own NFO token), validates every row, and upserts before
    re-reading from the DB -- so a caller always gets back exactly what
    is now actually stored, never a broker response that failed
    validation or a partial write.
    """
    rows = _stored_candles(contract, timeframe, from_dt, to_dt)
    if _is_fresh_enough(rows, timeframe, to_dt):
        return [_row_dict(r) for r in rows]

    from django.conf import settings

    if settings.BROKER_MODE != "live":
        # Paper mode / tests: no real broker session to fetch from --
        # serve whatever is already stored (possibly empty) rather than
        # raise, matching every other BROKER_MODE-guarded entry point's
        # "skip, don't crash" convention in this codebase.
        return [_row_dict(r) for r in rows]

    lock_key = f"options:candle_fetch:{contract.id}:{timeframe}"
    # A short stampede guard, not a hard block: if another request is
    # already fetching this exact contract+timeframe, this one just
    # serves whatever is already stored rather than piling on a second
    # concurrent Angel One call for the same data -- this platform has
    # real prior AB1021 rate-limit-cooldown incident history (see
    # apps.market_data.broker_client's own circuit-breaker comment), so
    # avoidable duplicate broker calls are worth preventing outright,
    # not just discouraging.
    if not cache.add(lock_key, True, timeout=FETCH_LOCK_TIMEOUT_SECONDS):
        return [_row_dict(r) for r in rows]

    try:
        from .broker_client import get_option_chain_client

        client = get_option_chain_client()
        lookback_days = DEFAULT_LOOKBACK_DAYS.get(timeframe, 5)
        if from_dt is not None:
            reference = to_dt or django_timezone.now()
            span_days = max(1, (django_timezone.localtime(reference) - django_timezone.localtime(from_dt)).days + 1)
            lookback_days = min(max(lookback_days, span_days), MAX_LOOKBACK_DAYS.get(timeframe, lookback_days))

        try:
            fetched = client.fetch_candles_for_token(
                OPTION_EXCHANGE, contract.symbol_token, timeframe, lookback_days=lookback_days, to_date=to_dt,
            )
        except Exception:
            logger.exception(
                "get_option_candles: Angel One fetch failed for contract_id=%s timeframe=%s -- "
                "serving whatever is already stored instead of failing the request outright.",
                contract.id, timeframe,
            )
            return [_row_dict(r) for r in rows]

        saved = 0
        for row in fetched:
            if not _validate_ohlc(row):
                logger.warning(
                    "get_option_candles: dropping invalid OHLC row for contract_id=%s: %r", contract.id, row,
                )
                continue
            try:
                # Nested atomic() -> a real SAVEPOINT, same reasoning as
                # apps.options.contract_sync's own per-contract upsert:
                # one bad row's IntegrityError must not poison the rest
                # of this batch's writes.
                with transaction.atomic():
                    OptionCandle.objects.update_or_create(
                        contract=contract, timeframe=timeframe, timestamp=row["timestamp"],
                        defaults={
                            "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"],
                            "volume": row["volume"], "source": "angel_one",
                        },
                    )
                saved += 1
            except IntegrityError:
                logger.warning(
                    "get_option_candles: integrity error upserting candle for contract_id=%s at %s",
                    contract.id, row["timestamp"],
                )
        if saved:
            rows = _stored_candles(contract, timeframe, from_dt, to_dt)
    finally:
        cache.delete(lock_key)

    return [_row_dict(r) for r in rows]
