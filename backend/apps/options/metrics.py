"""
Pure computation over OptionChainSnapshot rows -- no I/O, no broker
calls, so these are trivially unit-testable against a fixed set of
snapshot fixtures. apps.options.signals_engine and the risk/signal
engines are the callers; this module just does the math.
"""

from __future__ import annotations

from datetime import date

from django.db.models import Max, OuterRef, Subquery

from .models import OptionChainSnapshot, OptionContract


def _latest_snapshots(underlying: str, expiry: date):
    """
    One snapshot per contract -- the most recent one at or before "now"
    for that (underlying, expiry). Uses a correlated subquery (each
    contract's own latest timestamp) rather than fetching everything
    and filtering in Python, since a liquid index expiry can have 40+
    strikes x 2 (CE/PE) = 80+ contracts and this runs on every metrics
    call.
    """
    contracts = OptionContract.objects.filter(underlying=underlying, expiry=expiry)
    # OuterRef("contract_id") -- NOT OuterRef("pk") -- since this
    # subquery is correlated against the OUTER OptionChainSnapshot
    # query below (via Subquery(...)), "pk" there would resolve to the
    # outer SNAPSHOT's own id, not the contract it belongs to. That
    # exact mistake shipped here previously and only ever "worked" in
    # a test fixture where OptionContract and OptionChainSnapshot's
    # auto-increment counters happened to start in lockstep (both
    # fresh tables beginning at 1) -- any real dataset, where many
    # snapshots accumulate per contract over time, would silently
    # return zero rows from every caller (compute_pcr, compute_max_pain,
    # strike_support_resistance).
    latest_ts_per_contract = OptionChainSnapshot.objects.filter(
        contract_id=OuterRef("contract_id"),
    ).order_by("-timestamp").values("timestamp")[:1]

    return (
        OptionChainSnapshot.objects.filter(
            contract__in=contracts,
            timestamp=Subquery(latest_ts_per_contract),
        )
        .select_related("contract")
    )


def compute_pcr(underlying: str, expiry: date) -> float | None:
    """
    Put-Call Ratio by open interest: total PE OI / total CE OI. > 1
    conventionally read as bullish (more puts written than calls,
    often by option sellers betting the market won't fall that far),
    < 1 as bearish -- this function only computes the number; reading
    direction into it is signals_engine's job, not this one's.
    """
    snapshots = list(_latest_snapshots(underlying, expiry))
    if not snapshots:
        return None

    call_oi = sum(s.open_interest for s in snapshots if s.contract.option_type == "CE")
    put_oi = sum(s.open_interest for s in snapshots if s.contract.option_type == "PE")

    if call_oi == 0:
        return None
    return round(put_oi / call_oi, 4)


def compute_max_pain(underlying: str, expiry: date) -> float | None:
    """
    The strike at which option WRITERS (not buyers) would collectively
    lose the least money if the underlying settled there at expiry --
    conventionally watched as a magnet price near expiry ("expiry
    pinning", manual section 9's options-signal list) since a large
    fraction of open interest is written by institutions who may
    influence price toward it.

    Computed directly from the standard definition (total intrinsic
    value paid out to option BUYERS at each candidate settlement
    price, summed across every strike's OI) rather than any shortcut
    approximation, since max pain is exactly this calculation -- there
    isn't a simpler equivalent formula to fall back on.
    """
    snapshots = list(_latest_snapshots(underlying, expiry))
    if not snapshots:
        return None

    strikes = sorted({float(s.contract.strike) for s in snapshots})
    if not strikes:
        return None

    pain_by_settlement = {}
    for candidate_settlement in strikes:
        total_payout = 0.0
        for s in snapshots:
            strike = float(s.contract.strike)
            if s.contract.option_type == "CE" and candidate_settlement > strike:
                total_payout += (candidate_settlement - strike) * s.open_interest
            elif s.contract.option_type == "PE" and candidate_settlement < strike:
                total_payout += (strike - candidate_settlement) * s.open_interest
        pain_by_settlement[candidate_settlement] = total_payout

    # Max pain = the settlement price that MINIMIZES total payout to
    # buyers (i.e. writers' pain is minimized there) -- the name refers
    # to buyers' pain being maximized at that point, not writers'.
    return min(pain_by_settlement, key=pain_by_settlement.get)


def compute_iv_rank(current_iv: float, historical_ivs: list[float]) -> float | None:
    """
    Where current IV sits within its own recent historical range,
    0-100 (0 = lowest IV seen in the lookback, 100 = highest) -- this
    is IV RANK specifically, not IV percentile (rank uses the min/max
    range; percentile would count how many historical readings are
    below the current one). Both are used in practice; rank is what
    the manual's section 9 lists, so that's what's implemented.
    Returns None if there's no historical range to rank against yet
    (e.g. a freshly-added expiry with only one snapshot on record).
    """
    if not historical_ivs:
        return None
    iv_min, iv_max = min(historical_ivs), max(historical_ivs)
    if iv_max == iv_min:
        return None
    return round((current_iv - iv_min) / (iv_max - iv_min) * 100, 2)


def strike_support_resistance(underlying: str, expiry: date, top_n: int = 3) -> dict:
    """
    manual section 9: "strike-wise support/resistance" -- the
    convention is that strikes with the highest PUT OI act as support
    (put writers are betting price stays above there) and strikes with
    the highest CALL OI act as resistance (call writers betting price
    stays below there). Returns the top_n strikes by OI for each side.
    """
    snapshots = list(_latest_snapshots(underlying, expiry))
    if not snapshots:
        return {"support": [], "resistance": []}

    puts = sorted(
        (s for s in snapshots if s.contract.option_type == "PE"),
        key=lambda s: s.open_interest, reverse=True,
    )[:top_n]
    calls = sorted(
        (s for s in snapshots if s.contract.option_type == "CE"),
        key=lambda s: s.open_interest, reverse=True,
    )[:top_n]

    return {
        "support": [{"strike": float(s.contract.strike), "oi": s.open_interest} for s in puts],
        "resistance": [{"strike": float(s.contract.strike), "oi": s.open_interest} for s in calls],
    }
