"""
Advanced Greek Exposure: chain-wide, OI-weighted aggregations of
apps.options.greeks.py's own Black-Scholes output -- gamma exposure,
plus vanna/charm derived from it via finite differences (this module
adds no new pricing model; it bumps compute_greeks' own inputs and
re-reads delta).

EVERY function here returns a `"label": "MODELED"` field and an
`"assumption"` string. This is not a hedge-your-bets disclaimer -- it
is the literal truth of what these numbers are: this platform has no
feed of actual dealer/market-maker positions (confirmed: no such data
source exists anywhere in this codebase), so "gamma exposure" here is
a PROXY built entirely from observable chain data (OI + Greeks) under
a documented, standard, but unverified assumption about how dealers
are positioned relative to customer OI. Never present these numbers as
"dealer gamma is X" without that caveat traveling with them.
"""

from __future__ import annotations

from datetime import date

from django.utils import timezone

# The convention widely used by retail options-flow-analytics tools
# (e.g. the "SpotGamma"-style net-GEX calculation): customers are
# assumed net LONG both calls and puts (the buy-side is the visible,
# OI-generating side for a retail/index options market), so dealers/
# market-makers who sold that OI are assumed net SHORT calls and net
# SHORT puts. A dealer short a call has NEGATIVE gamma exposure from
# that position; a dealer short a put also has POSITIVE delta exposure
# but the standard GEX convention still assigns puts a POSITIVE sign
# to net gamma (offsetting calls) because a dealer who is short a put
# hedges by buying the underlying as it falls, which is gamma-positive
# hedging behavior from the dealer's book -- this sign convention is
# the industry-standard one, not invented here, but it is still an
# ASSUMPTION about who holds which side of the OI, not observed fact.
DEALER_POSITIONING_ASSUMPTION = (
    "MODELED: assumes dealers/market-makers are net short the OI customers "
    "are net long (calls and puts both) -- the standard convention "
    "options-flow-analytics tools use, NOT verified against any real "
    "dealer book (no such data source exists for this platform). Treat as "
    "a chain-derived positioning PROXY, never as known fact about actual "
    "dealer positions."
)

VANNA_IV_BUMP = 0.01  # 1 percentage point of IV (decimal), for the finite-difference derivative
CHARM_DAY_BUMP_YEARS = 1.0 / 365.0  # exactly one calendar day of time decay


def _scoreable_contracts(underlying: str, expiry: date, spot: float) -> list[dict]:
    """
    Every contract (both sides) with a real snapshot, OI, and a
    solvable IV -- the shared candidate list every exposure function
    below aggregates over. Carries the solved sigma (decimal) and
    tte_years alongside the greeks so vanna/charm can re-call
    compute_greeks with bumped inputs without re-solving IV.
    """
    from . import metrics
    from .greeks import DEFAULT_RISK_FREE_RATE, compute_greeks_for_contract
    from .models import OptionContract

    contracts = OptionContract.objects.filter(underlying=underlying, expiry=expiry)
    latest_by_contract = {s.contract_id: s for s in metrics._latest_snapshots(underlying, expiry)}
    today = timezone.localdate()

    out = []
    for contract in contracts:
        snapshot = latest_by_contract.get(contract.pk)
        if snapshot is None or snapshot.ltp is None or snapshot.open_interest is None:
            continue
        greeks = compute_greeks_for_contract(contract, spot, float(snapshot.ltp))
        if greeks is None:
            continue
        tte_years = (contract.expiry - today).days / 365.0
        out.append({
            "contract": contract, "option_type": contract.option_type,
            "open_interest": snapshot.open_interest, "lot_size": contract.lot_size or 1,
            "sigma": greeks["iv"] / 100.0, "tte_years": tte_years, "greeks": greeks,
        })
    return out


def calculate_net_gamma_exposure(underlying: str, expiry: date, spot: float) -> dict:
    """
    Net $-gamma-exposure-per-1%-move proxy: for each contract,
    gamma * open_interest * lot_size * spot^2 * 0.01, signed by
    DEALER_POSITIONING_ASSUMPTION's convention (calls negative, puts
    positive to net dealer gamma), summed across the whole chain.

    Returns {"net_gamma_exposure", "call_gamma_exposure",
    "put_gamma_exposure", "label": "MODELED", "assumption": str,
    "contracts_used": int}. All exposure values are None (not 0) if no
    contract was scoreable -- 0 would falsely imply "measured and flat".
    """
    candidates = _scoreable_contracts(underlying, expiry, spot)
    if not candidates:
        return {
            "net_gamma_exposure": None, "call_gamma_exposure": None, "put_gamma_exposure": None,
            "label": "MODELED", "assumption": DEALER_POSITIONING_ASSUMPTION, "contracts_used": 0,
        }

    call_exposure = 0.0
    put_exposure = 0.0
    for c in candidates:
        notional = c["greeks"]["gamma"] * c["open_interest"] * c["lot_size"] * spot * spot * 0.01
        if c["option_type"] == "CE":
            call_exposure += notional
        else:
            put_exposure += notional

    return {
        "net_gamma_exposure": round(-call_exposure + put_exposure, 2),
        "call_gamma_exposure": round(call_exposure, 2),
        "put_gamma_exposure": round(put_exposure, 2),
        "label": "MODELED", "assumption": DEALER_POSITIONING_ASSUMPTION, "contracts_used": len(candidates),
    }


def _aggregate_second_order_exposure(underlying: str, expiry: date, spot: float, delta_at) -> dict:
    """
    Shared aggregation for vanna/charm: `delta_at(candidate, bumped_sigma, bumped_tte)`
    supplies the finite-difference derivative for one candidate; this
    function OI/lot-weights and dealer-mirror-signs it exactly like
    calculate_net_gamma_exposure, so the two second-order Greeks report
    in the same shape/convention as the first-order one.
    """
    candidates = _scoreable_contracts(underlying, expiry, spot)
    if not candidates:
        return {"net_exposure": None, "call_exposure": None, "put_exposure": None, "contracts_used": 0}

    call_exposure = 0.0
    put_exposure = 0.0
    used = 0
    for c in candidates:
        derivative = delta_at(c)
        if derivative is None:
            continue
        notional = derivative * c["open_interest"] * c["lot_size"]
        if c["option_type"] == "CE":
            call_exposure += notional
        else:
            put_exposure += notional
        used += 1

    if used == 0:
        return {"net_exposure": None, "call_exposure": None, "put_exposure": None, "contracts_used": 0}
    return {
        "net_exposure": round(-call_exposure + put_exposure, 4),
        "call_exposure": round(call_exposure, 4), "put_exposure": round(put_exposure, 4),
        "contracts_used": used,
    }


def calculate_vanna_exposure(underlying: str, expiry: date, spot: float) -> dict:
    """
    Vanna = dDelta/dVol -- how much a contract's delta shifts per 1
    percentage point of IV, derived by finite difference (bump sigma by
    +/-VANNA_IV_BUMP around the contract's own solved IV, re-run
    apps.options.greeks.compute_greeks, difference the two deltas). No
    new pricing model: this bumps and re-reads the SAME Black-Scholes
    Greeks already used everywhere else in apps.options, so it inherits
    exactly the same assumptions/limits greeks.py's own docstring
    already documents -- nothing new is added by taking a derivative of it.
    """
    from .greeks import DEFAULT_RISK_FREE_RATE, compute_greeks

    def delta_at(c):
        strike = float(c["contract"].strike)
        up = compute_greeks(spot, strike, c["tte_years"], DEFAULT_RISK_FREE_RATE, c["sigma"] + VANNA_IV_BUMP, c["option_type"])
        down = compute_greeks(spot, strike, c["tte_years"], DEFAULT_RISK_FREE_RATE, max(c["sigma"] - VANNA_IV_BUMP, 1e-5), c["option_type"])
        if up is None or down is None:
            return None
        return (up["delta"] - down["delta"]) / (2 * VANNA_IV_BUMP)

    result = _aggregate_second_order_exposure(underlying, expiry, spot, delta_at)
    return {**result, "label": "MODELED", "assumption": DEALER_POSITIONING_ASSUMPTION}


def calculate_charm_exposure(underlying: str, expiry: date, spot: float) -> dict:
    """
    Charm = how much delta decays as ONE calendar day passes (time
    moves forward, time-to-expiry shrinks by CHARM_DAY_BUMP_YEARS),
    holding spot/IV fixed -- delta(tte - 1 day) - delta(tte), same
    finite-difference approach as vanna, same "no new model" caveat.
    Skips (excludes, doesn't crash) any contract expiring within a day.
    """
    from .greeks import DEFAULT_RISK_FREE_RATE, compute_greeks

    def delta_at(c):
        bumped_tte = c["tte_years"] - CHARM_DAY_BUMP_YEARS
        if bumped_tte <= 0:
            return None
        strike = float(c["contract"].strike)
        now_g = compute_greeks(spot, strike, c["tte_years"], DEFAULT_RISK_FREE_RATE, c["sigma"], c["option_type"])
        later_g = compute_greeks(spot, strike, bumped_tte, DEFAULT_RISK_FREE_RATE, c["sigma"], c["option_type"])
        if now_g is None or later_g is None:
            return None
        return later_g["delta"] - now_g["delta"]

    result = _aggregate_second_order_exposure(underlying, expiry, spot, delta_at)
    return {**result, "label": "MODELED", "assumption": DEALER_POSITIONING_ASSUMPTION}
