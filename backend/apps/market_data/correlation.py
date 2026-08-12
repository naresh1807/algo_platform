"""
Correlation matrix across settings.WATCHLIST (manual section 13: "Max
correlated exposure: 2-3%" -- this is the piece that was missing to
enforce that rule, flagged as a known gap in both this app's
regime.py and apps/risk/engine.py since Phase 2).

Kept deliberately simple: a Pearson correlation over recent daily
closes, recomputed on demand (not cached/scheduled) since the
watchlist is small (2 symbols by default) and this is cheap to compute
-- if the watchlist grows to dozens of symbols, this should move to a
scheduled Celery task that caches the matrix rather than recomputing
per risk check, but that's premature for the current scale.
"""

from __future__ import annotations

import pandas as pd
from django.conf import settings

from .models import HistoricalData

CORRELATION_LOOKBACK_DAYS = 60
HIGH_CORRELATION_THRESHOLD = 0.7  # |correlation| >= this counts as "correlated" for exposure purposes


def _closing_prices_by_symbol(timeframe: str = "1d") -> pd.DataFrame:
    """
    Returns a DataFrame with one column per watchlist symbol, aligned by
    timestamp (an outer join, so a symbol missing a day doesn't drop
    that day for every other symbol -- correlation is computed with
    pandas' pairwise NaN handling instead).
    """
    frames = {}
    for symbol in settings.WATCHLIST:
        rows = list(
            HistoricalData.objects.filter(symbol=symbol, timeframe=timeframe)
            .order_by("-timestamp")[:CORRELATION_LOOKBACK_DAYS]
            .values("timestamp", "close")
        )
        if not rows:
            continue
        series = pd.Series(
            {r["timestamp"]: float(r["close"]) for r in rows}, name=symbol,
        ).sort_index()
        frames[symbol] = series

    if len(frames) < 2:
        return pd.DataFrame()

    return pd.DataFrame(frames)


def compute_correlation_matrix(timeframe: str = "1d") -> pd.DataFrame:
    """
    Returns a symbol x symbol Pearson correlation matrix of daily
    returns (not raw prices -- correlating price levels is misleading
    for anything with a trend, since two trending-up symbols look
    highly "correlated" even if their day-to-day moves are unrelated;
    returns are the standard, correct basis for this).

    Returns an empty DataFrame if there isn't enough overlapping data
    yet for at least 2 symbols -- callers must handle that (treat as
    "unknown, don't block on it" rather than crashing), same
    "not enough data yet is a normal state" pattern used throughout
    apps.market_data.indicators.
    """
    prices = _closing_prices_by_symbol(timeframe)
    if prices.empty:
        return pd.DataFrame()

    returns = prices.pct_change().dropna(how="all")
    return returns.corr()


def correlated_symbols(symbol: str, timeframe: str = "1d") -> list[str]:
    """
    Returns other watchlist symbols whose |correlation| with `symbol`
    is at or above HIGH_CORRELATION_THRESHOLD. Used by
    apps.risk.engine to decide whether an open position in a correlated
    symbol should count toward `symbol`'s effective exposure.
    """
    matrix = compute_correlation_matrix(timeframe)
    if matrix.empty or symbol not in matrix.columns:
        return []

    correlations = matrix[symbol].drop(labels=[symbol], errors="ignore")
    return correlations[correlations.abs() >= HIGH_CORRELATION_THRESHOLD].index.tolist()
