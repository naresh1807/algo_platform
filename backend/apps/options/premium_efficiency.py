"""
Option Premium Efficiency: compares a contract's actual traded premium
against a THEORETICAL value derived from the underlying's own REALIZED
(historical) volatility -- not the IV solved from that same market
premium, which would trivially reproduce the market price with zero
deviation and tell you nothing. Comparing market IV against realized
vol is the standard "is this option rich or cheap relative to how much
the underlying has actually been moving" read; apps.options.greeks
already handles the Black-Scholes math itself, this module supplies
the independent volatility input and the intrinsic/time-value split.
"""

from __future__ import annotations

import math

from django.utils import timezone


def calculate_intrinsic_value(spot: float, strike: float, option_type: str) -> float:
    """Standard intrinsic value -- max(0, spot-strike) for a call, max(0, strike-spot) for a put."""
    if option_type == "CE":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def calculate_time_value(option_price: float, intrinsic_value: float) -> float:
    """What's left of the premium once intrinsic value is subtracted out -- never negative (a real quote trading below intrinsic is a bad-tick/arbitrage flag, handled by apps.options.data_quality, not silently negative here)."""
    return max(0.0, option_price - intrinsic_value)


def calculate_realized_volatility(symbol: str, timeframe: str = "1d", lookback_days: int = 20) -> float | None:
    """
    Annualized realized (historical) volatility from the underlying's
    own log returns -- the standard close-to-close estimator:
    stdev(daily log returns) * sqrt(252). Real statistics over
    already-ingested apps.market_data.HistoricalData candles, not an
    external data source. Returns None (not a guess) with fewer than 2
    usable closes in the lookback window.
    """
    from apps.market_data.models import HistoricalData

    candles = list(
        HistoricalData.objects.filter(symbol=symbol, timeframe=timeframe)
        .order_by("-timestamp")[:lookback_days + 1]
    )
    closes = [float(c.close) for c in reversed(candles) if c.close and c.close > 0]
    if len(closes) < 3:
        return None

    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(log_returns) < 2:
        return None

    mean_return = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_return) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_std = math.sqrt(variance)
    return round(daily_std * math.sqrt(252) * 100, 4)  # annualized, as a percentage (matches IV's own units)


def calculate_premium_deviation(market_price: float | None, theoretical_value: float | None) -> dict:
    """
    Returns {"deviation_abs", "deviation_pct", "richness"}. richness is
    "rich" (market > theoretical by >5%), "cheap" (market < theoretical
    by >5%), or "fair" (within +/-5%) -- a descriptive label, not a
    trade signal on its own; 5% is a documented, tunable constant, not
    hidden in the arithmetic.
    """
    if market_price is None or theoretical_value is None or theoretical_value <= 0:
        return {"deviation_abs": None, "deviation_pct": None, "richness": "unavailable"}

    deviation_abs = market_price - theoretical_value
    deviation_pct = deviation_abs / theoretical_value * 100
    if deviation_pct > 5:
        richness = "rich"
    elif deviation_pct < -5:
        richness = "cheap"
    else:
        richness = "fair"
    return {"deviation_abs": round(deviation_abs, 2), "deviation_pct": round(deviation_pct, 2), "richness": richness}


def calculate_premium_deviation_for_contract(contract, spot: float, market_price: float) -> dict:
    """
    The real, callable version: resolves the underlying's realized
    volatility from `contract.underlying`, prices the contract via
    Black-Scholes with THAT sigma (not the contract's own solved IV),
    and reports intrinsic/time value plus the deviation vs. that
    independent theoretical price. Returns None fields throughout if
    realized vol can't be computed yet (not enough daily history).
    """
    from .greeks import DEFAULT_RISK_FREE_RATE, black_scholes_price

    strike = float(contract.strike)
    intrinsic = calculate_intrinsic_value(spot, strike, contract.option_type)
    time_value = calculate_time_value(market_price, intrinsic)

    realized_vol_pct = calculate_realized_volatility(contract.underlying)
    if realized_vol_pct is None:
        return {
            "intrinsic_value": round(intrinsic, 2), "time_value": round(time_value, 2),
            "theoretical_value": None, "realized_vol_pct": None,
            "deviation_abs": None, "deviation_pct": None, "richness": "unavailable",
        }

    tte_years = (contract.expiry - timezone.localdate()).days / 365.0
    theoretical_value = black_scholes_price(
        spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, realized_vol_pct / 100.0, contract.option_type,
    )
    deviation = calculate_premium_deviation(market_price, theoretical_value)

    return {
        "intrinsic_value": round(intrinsic, 2), "time_value": round(time_value, 2),
        "theoretical_value": round(theoretical_value, 2) if theoretical_value is not None else None,
        "realized_vol_pct": realized_vol_pct,
        **deviation,
    }
