"""
Read-only market-data access for the autonomous paper-trading loop --
no new ingestion pipeline, this only reads what apps.market_data /
apps.options already write (their own live feed / Celery tasks).

Deliberately reads OptionChainSnapshot's already-ingested latest row
for contract quotes (bid/ask/ltp/oi/volume) rather than making a fresh
Angel One quote call per decision tick -- the same source
apps.options.strike_selector.suggest_best_strike already uses, so this
never adds broker-rate-limit pressure on top of the existing live feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.utils import timezone as django_timezone


@dataclass
class UnderlyingSnapshot:
    spot: float
    as_of: datetime
    age_seconds: float


@dataclass
class ContractQuote:
    contract_id: int
    ltp: float | None
    bid: float | None
    ask: float | None
    open_interest: int | None
    volume: int | None
    as_of: datetime
    age_seconds: float


def latest_underlying_snapshot(underlying: str) -> UnderlyingSnapshot | None:
    from apps.market_data.models import HistoricalData

    row = HistoricalData.objects.filter(symbol=underlying).order_by("-timestamp").first()
    if row is None:
        return None
    now = django_timezone.now()
    return UnderlyingSnapshot(
        spot=float(row.close),
        as_of=row.timestamp,
        age_seconds=(now - row.timestamp).total_seconds(),
    )


def latest_contract_quote(contract) -> ContractQuote | None:
    from apps.options import metrics

    snapshot = (
        metrics._latest_snapshots(contract.underlying, contract.expiry)
    )
    match = next((s for s in snapshot if s.contract_id == contract.pk), None)
    if match is None:
        return None
    now = django_timezone.now()
    return ContractQuote(
        contract_id=contract.pk,
        ltp=float(match.ltp) if match.ltp is not None else None,
        bid=float(match.bid) if match.bid is not None else None,
        ask=float(match.ask) if match.ask is not None else None,
        open_interest=match.open_interest,
        volume=match.volume,
        as_of=match.timestamp,
        age_seconds=(now - match.timestamp).total_seconds(),
    )


def latest_completed_candle_frame(symbol: str, timeframe: str, limit: int = 300):
    """Leakage-safe: the current, still-forming candle is always dropped.
    Returns the same pandas DataFrame shape apps.market_data.indicators
    uses everywhere else (open/high/low/close/volume + ema9/ema21/...
    once indicator columns are applied by the caller)."""
    from apps.market_data.indicators import _drop_unclosed_last_bar, load_candle_frame

    df = load_candle_frame(symbol, timeframe, limit=limit)
    return _drop_unclosed_last_bar(df, timeframe)
