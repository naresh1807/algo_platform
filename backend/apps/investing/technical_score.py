"""
"Technical analysis is important too" -- the trader asked for this
explicitly, on top of fundamentals + valuation. This is deliberately
much simpler than apps.market_data.indicators (which drives INTRADAY
entry/exit timing off 5m candles for the index watchlist) -- for a
holding-period-measured-in-months decision, what matters is the
DAILY-timeframe trend (is this stock in a real uptrend or downtrend
right now), not intraday noise. Two classic, well-understood signals:

  1. Moving-average trend: price vs SMA50 vs SMA200 (the "golden
     cross" / "death cross" read every technical trader recognizes).
  2. RSI-14 on daily closes -- reuses apps.market_data.indicators._rsi
     (the exact same Wilder-smoothed formula the intraday engine uses,
     just fed daily closes instead of 5m ones) rather than
     reimplementing RSI a second time with a chance of subtly
     disagreeing with it.

Needs real apps.investing.models.StockPriceHistory rows (from
apps.investing.fundamentals_client.get_historical_prices) -- returns
None rather than a guess when there isn't enough history yet.
"""

from __future__ import annotations

MIN_DAYS_FOR_SMA50 = 50
MIN_DAYS_FOR_SMA200 = 200


def compute_technical_score(stock) -> dict | None:
    """
    Returns {"score": 0-100, "trend": label, "rsi": float|None,
    "price": float, "sma50": float|None, "sma200": float|None,
    "detail": str} or None if there isn't even MIN_DAYS_FOR_SMA50 days
    of real price history yet.
    """
    import pandas as pd

    from apps.market_data.indicators import _rsi

    bars = list(stock.price_history.order_by("date").values("date", "close"))
    if len(bars) < MIN_DAYS_FOR_SMA50:
        return None

    closes = pd.Series([float(b["close"]) for b in bars])
    price = closes.iloc[-1]

    sma50 = closes.rolling(window=50).mean().iloc[-1]
    sma200 = closes.rolling(window=200).mean().iloc[-1] if len(closes) >= MIN_DAYS_FOR_SMA200 else None

    rsi_series = _rsi(closes, length=14)
    rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else None

    trend, trend_score = _trend_read(price, sma50, sma200)
    rsi_score = _rsi_score(rsi)

    # Trend carries more weight than RSI here -- RSI is a momentum/
    # overbought-oversold gauge, useful context but secondary to "is
    # this actually in an uptrend" for a months-long hold decision.
    if rsi_score is not None:
        score = round(trend_score * 0.7 + rsi_score * 0.3, 1)
    else:
        score = round(trend_score, 1)

    detail_bits = [f"price {price:.2f} vs SMA50 {sma50:.2f}" + (f" vs SMA200 {sma200:.2f}" if sma200 else " (SMA200 not enough history yet)")]
    if rsi is not None:
        detail_bits.append(f"RSI-14 {rsi:.1f}")
    detail = ", ".join(detail_bits) + f" -> {trend}."

    return {
        "score": score, "trend": trend, "rsi": rsi, "price": price,
        "sma50": float(sma50) if sma50 == sma50 else None,  # NaN check
        "sma200": float(sma200) if sma200 is not None and sma200 == sma200 else None,
        "detail": detail,
    }


def _trend_read(price: float, sma50: float, sma200: float | None) -> tuple[str, float]:
    if sma200 is not None:
        if price > sma50 > sma200:
            return "Strong Uptrend", 95.0   # classic golden-cross alignment
        if price > sma50 and price > sma200:
            return "Uptrend", 75.0
        if price < sma50 < sma200:
            return "Strong Downtrend", 5.0  # classic death-cross alignment
        if price < sma50 and price < sma200:
            return "Downtrend", 25.0
        return "Sideways / Mixed", 50.0

    # No SMA200 yet (less than 200 days of history) -- read off SMA50 alone.
    if price > sma50 * 1.03:
        return "Uptrend", 75.0
    if price < sma50 * 0.97:
        return "Downtrend", 25.0
    return "Sideways / Mixed", 50.0


def _rsi_score(rsi: float | None) -> float | None:
    """
    RSI 45-60 (healthy uptrend momentum, not yet overbought) scores
    highest; very overbought (>75) or very oversold (<25) both pull
    the score down -- overbought because a pullback is more likely
    near-term, oversold because it often means the downtrend the SMA
    read already penalized is accelerating, not that it's "cheap."
    """
    if rsi is None:
        return None
    if 45 <= rsi <= 60:
        return 90.0
    if 30 <= rsi < 45 or 60 < rsi <= 70:
        return 65.0
    if rsi > 70:
        return 35.0
    return 30.0  # rsi < 30
