"""
Black-Scholes Greeks + implied volatility. Fills the gap
apps/options/broker_client.py's module docstring documents honestly:
Angel One's standard quote payload doesn't reliably include IV, and a
dedicated OptionGreek broker endpoint would be needed to get it
directly. This module computes IV locally instead -- from the option's
own traded price (LTP), which every quote DOES include -- so Greeks
don't depend on that broker endpoint existing or being wired up at all.

This is standard options-pricing math (European Black-Scholes; NSE
index/stock options are American-style but are commonly priced with
Black-Scholes as a close approximation for this kind of dashboard
analytics, not for market-making) -- implemented directly with `math`
(erf-based normal CDF/PDF) rather than pulling in scipy, since this is
the only place in the codebase that would need it.

Sign/unit conventions:
- S, K in price units (e.g. INR). T in YEARS (not days).
- sigma (volatility) and the risk-free rate r are both ANNUALIZED
  decimals (0.15 = 15%), matching how IV is quoted everywhere else in
  this app (OptionChainSnapshot.iv is stored as a percentage, so
  callers divide by 100 before passing sigma here -- see
  compute_greeks_for_contract below).
- theta is returned per CALENDAR DAY (annual theta / 365), since that's
  how traders actually read it ("this position loses X per day"), not
  per year.
"""

from __future__ import annotations

import math

# manual gives no explicit risk-free-rate figure anywhere (see this
# module's sibling files' own "no manual spec for this" notes) --
# documented assumption: a round, roughly-current INR risk-free proxy
# (T-bill-adjacent), not fetched live. Wrong by a percent or two makes
# a negligible difference to Greeks for typical NSE index-option
# expiries (days to a few months out); it is NOT precise enough to
# use for anything beyond dashboard-level analytics.
DEFAULT_RISK_FREE_RATE = 0.065

_SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1_d2(spot: float, strike: float, tte_years: float, rate: float, sigma: float) -> tuple[float, float]:
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * tte_years) / (sigma * math.sqrt(tte_years))
    d2 = d1 - sigma * math.sqrt(tte_years)
    return d1, d2


def black_scholes_price(
    spot: float, strike: float, tte_years: float, rate: float, sigma: float, option_type: str,
) -> float | None:
    """
    Theoretical option price. Returns None for degenerate inputs
    (expired/zero time, non-positive spot/strike/sigma) rather than
    raising or returning a nonsense number -- callers (the IV solver
    below) treat None the same "can't compute this yet" way the rest
    of apps.options treats missing data.
    """
    if tte_years <= 0 or spot <= 0 or strike <= 0 or sigma <= 0:
        return None
    d1, d2 = _d1_d2(spot, strike, tte_years, rate, sigma)
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * tte_years) * _norm_cdf(d2)
    return strike * math.exp(-rate * tte_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_volatility(
    option_price: float, spot: float, strike: float, tte_years: float, rate: float, option_type: str,
    tolerance: float = 1e-4, max_iterations: int = 100,
) -> float | None:
    """
    Solves for sigma via bisection (robust and dependency-free, unlike
    Newton-Raphson which can diverge on deep ITM/OTM contracts near
    expiry) over a wide, deliberately generous bracket [0.001%, 500%]
    -- NSE index options can legitimately show IV well above 100%
    around events. Returns None if the price is outside the
    no-arbitrage bounds for any volatility (a bad/stale LTP tick, or a
    contract too close to expiry for a stable solve) rather than
    returning a wrong number.
    """
    if tte_years <= 0 or spot <= 0 or strike <= 0 or option_price <= 0:
        return None

    low, high = 1e-5, 5.0
    price_at_low = black_scholes_price(spot, strike, tte_years, rate, low, option_type)
    price_at_high = black_scholes_price(spot, strike, tte_years, rate, high, option_type)
    if price_at_low is None or price_at_high is None:
        return None
    if not (price_at_low <= option_price <= price_at_high):
        return None  # price outside the achievable range at any vol -- stale/bad tick

    for _ in range(max_iterations):
        mid = (low + high) / 2
        price_at_mid = black_scholes_price(spot, strike, tte_years, rate, mid, option_type)
        if price_at_mid is None:
            return None
        diff = price_at_mid - option_price
        if abs(diff) < tolerance:
            return round(mid, 6)
        if diff > 0:
            high = mid
        else:
            low = mid
    return round((low + high) / 2, 6)


def compute_greeks(
    spot: float, strike: float, tte_years: float, rate: float, sigma: float, option_type: str,
) -> dict | None:
    """
    Returns {"delta", "gamma", "theta", "vega", "rho"} or None for
    degenerate inputs (same guard as black_scholes_price). theta is
    per CALENDAR DAY (see module docstring); vega and rho are per 1%
    (0.01) change in volatility / rate respectively -- the conventional
    trader units, not the raw per-unit derivative.
    """
    if tte_years <= 0 or spot <= 0 or strike <= 0 or sigma <= 0:
        return None

    d1, d2 = _d1_d2(spot, strike, tte_years, rate, sigma)
    sqrt_t = math.sqrt(tte_years)
    discount = math.exp(-rate * tte_years)

    gamma = _norm_pdf(d1) / (spot * sigma * sqrt_t)
    vega = spot * _norm_pdf(d1) * sqrt_t * 0.01  # per 1% vol change

    if option_type == "CE":
        delta = _norm_cdf(d1)
        theta_annual = (
            -(spot * _norm_pdf(d1) * sigma) / (2 * sqrt_t) - rate * strike * discount * _norm_cdf(d2)
        )
        rho = strike * tte_years * discount * _norm_cdf(d2) * 0.01
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (
            -(spot * _norm_pdf(d1) * sigma) / (2 * sqrt_t) + rate * strike * discount * _norm_cdf(-d2)
        )
        rho = -strike * tte_years * discount * _norm_cdf(-d2) * 0.01

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta_annual / 365.0, 4),  # per calendar day
        "vega": round(vega, 4),
        "rho": round(rho, 4),
    }


def compute_greeks_for_contract(contract, spot: float, option_price: float) -> dict | None:
    """
    Convenience wrapper for the common call shape: given an
    OptionContract and the underlying's current spot price (see
    apps.options.signals_engine._latest_underlying_ltp) plus the
    contract's own latest traded price, solves IV from that price and
    returns both the IV and the full Greeks -- {"iv", "delta", "gamma",
    "theta", "vega", "rho"}, or None if IV couldn't be solved (see
    implied_volatility's own None cases).
    """
    from django.utils import timezone

    tte_years = (contract.expiry - timezone.localdate()).days / 365.0
    strike = float(contract.strike)

    iv = implied_volatility(
        option_price, spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, contract.option_type,
    )
    if iv is None:
        return None

    greeks = compute_greeks(spot, strike, tte_years, DEFAULT_RISK_FREE_RATE, iv, contract.option_type)
    if greeks is None:
        return None

    return {"iv": round(iv * 100, 2), **greeks}  # iv returned as a percentage, matching OptionChainSnapshot.iv
