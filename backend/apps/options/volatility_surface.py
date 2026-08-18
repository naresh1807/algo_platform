"""
Volatility Surface Engine: IV percentile, skew, and term structure --
everything here reads real, already-ingested data (apps.options.
OptionChainSnapshot's IV history, now covering multiple synced expiries
per underlying since apps.options.tasks.sync_watchlist_option_contracts
was widened to SYNC_EXPIRY_COUNT expiries) and greeks.py's own
Black-Scholes/IV-solver output. Nothing here uses a fixed rule like
"high IV = bearish" -- these are descriptive statistics of the surface,
consumed by apps.options.confirmation (a later phase) alongside every
other factor, not a standalone signal.

Skew convention used throughout (standard options-market convention,
not invented here): skew is measured as an IV DIFFERENCE between an
out-of-the-money contract and the at-the-money contract on the same
side, and the "25-delta skew" (a common single-number risk-reversal
summary) is IV(25-delta put) - IV(25-delta call) -- index options
typically price this positive (puts richer than calls, "volatility
smirk"), but this module reports the number, it does not assert what a
given sign means for direction.
"""

from __future__ import annotations

from datetime import date

from django.utils import timezone


def calculate_iv_percentile(current_iv: float | None, historical_ivs: list[float]) -> float | None:
    """
    Percentile-of-historical-readings -- deliberately distinct from
    apps.options.metrics.compute_iv_rank (a min/max RANGE-based rank).
    Percentile answers "what fraction of past readings were at or below
    today's IV", which is less sensitive to a single extreme outlier in
    the historical window than a min/max range is.

    Returns None (not a guess) if there's no current IV or no history.
    """
    if current_iv is None or not historical_ivs:
        return None
    count_at_or_below = sum(1 for iv in historical_ivs if iv <= current_iv)
    return round(count_at_or_below / len(historical_ivs) * 100, 2)


def _contracts_with_greeks(underlying: str, expiry: date, option_type: str, spot: float) -> list[dict]:
    """
    Every contract of one option_type for this underlying+expiry with a
    real snapshot and a solvable IV, each carrying its own computed
    greeks -- the shared candidate list every skew/ATM function below
    selects from. Mirrors apps.options.strike_selector.suggest_best_
    strike's own candidate-building loop (same bulk _latest_snapshots
    fetch, same "skip if snapshot/IV missing" discipline) rather than a
    second, divergent copy of that logic.
    """
    from . import metrics
    from .greeks import compute_greeks_for_contract
    from .models import OptionContract

    contracts = OptionContract.objects.filter(underlying=underlying, expiry=expiry, option_type=option_type)
    latest_by_contract = {s.contract_id: s for s in metrics._latest_snapshots(underlying, expiry)}

    out = []
    for contract in contracts:
        snapshot = latest_by_contract.get(contract.pk)
        if snapshot is None or snapshot.ltp is None:
            continue
        greeks = compute_greeks_for_contract(contract, spot, float(snapshot.ltp))
        if greeks is None:
            continue
        out.append({"contract": contract, "strike": float(contract.strike), "iv": greeks["iv"], "delta": greeks["delta"]})
    return out


def _nearest_by_abs_delta(candidates: list[dict], target_abs_delta: float) -> dict | None:
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(abs(c["delta"]) - target_abs_delta))


def _nearest_to_spot(candidates: list[dict], spot: float) -> dict | None:
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c["strike"] - spot))


def calculate_atm_iv(underlying: str, expiry: date, spot: float) -> dict:
    """
    ATM IV from BOTH sides (a real chain's CE and PE at the same strike
    rarely have identical IV -- put-call IV parity holds only under
    idealized assumptions this platform doesn't assume). Returns
    {"call_iv", "put_iv", "average_iv", "strike"} with any side left
    None if that side has no scoreable contract, never a fabricated
    average from one side alone.
    """
    calls = _contracts_with_greeks(underlying, expiry, "CE", spot)
    puts = _contracts_with_greeks(underlying, expiry, "PE", spot)
    atm_call = _nearest_to_spot(calls, spot)
    atm_put = _nearest_to_spot(puts, spot)

    call_iv = atm_call["iv"] if atm_call else None
    put_iv = atm_put["iv"] if atm_put else None
    if call_iv is not None and put_iv is not None:
        average_iv = round((call_iv + put_iv) / 2, 4)
    else:
        average_iv = call_iv if call_iv is not None else put_iv

    return {
        "call_iv": call_iv, "put_iv": put_iv, "average_iv": average_iv,
        "strike": atm_call["strike"] if atm_call else (atm_put["strike"] if atm_put else None),
    }


def calculate_call_skew(underlying: str, expiry: date, spot: float, otm_target_abs_delta: float = 0.25) -> float | None:
    """IV(OTM call near otm_target_abs_delta) - IV(ATM call). None if either side is unavailable."""
    calls = _contracts_with_greeks(underlying, expiry, "CE", spot)
    atm = _nearest_to_spot(calls, spot)
    otm = _nearest_by_abs_delta(calls, otm_target_abs_delta)
    if atm is None or otm is None or atm["iv"] is None or otm["iv"] is None:
        return None
    return round(otm["iv"] - atm["iv"], 4)


def calculate_put_skew(underlying: str, expiry: date, spot: float, otm_target_abs_delta: float = 0.25) -> float | None:
    """IV(OTM put near otm_target_abs_delta) - IV(ATM put). None if either side is unavailable."""
    puts = _contracts_with_greeks(underlying, expiry, "PE", spot)
    atm = _nearest_to_spot(puts, spot)
    otm = _nearest_by_abs_delta(puts, otm_target_abs_delta)
    if atm is None or otm is None or atm["iv"] is None or otm["iv"] is None:
        return None
    return round(otm["iv"] - atm["iv"], 4)


def calculate_25_delta_skew(underlying: str, expiry: date, spot: float) -> float | None:
    """
    The classic single-number risk-reversal skew: IV(25-delta put) -
    IV(25-delta call). No literal "25-delta strike" is ever listed by a
    broker -- this picks whichever synced strike's own computed |delta|
    is closest to 0.25 on each side, same nearest-match approach
    real trading desks use when an exact-delta strike doesn't exist.
    """
    calls = _contracts_with_greeks(underlying, expiry, "CE", spot)
    puts = _contracts_with_greeks(underlying, expiry, "PE", spot)
    call_25d = _nearest_by_abs_delta(calls, 0.25)
    put_25d = _nearest_by_abs_delta(puts, 0.25)
    if call_25d is None or put_25d is None or call_25d["iv"] is None or put_25d["iv"] is None:
        return None
    return round(put_25d["iv"] - call_25d["iv"], 4)


def calculate_atm_to_otm_iv_difference(underlying: str, expiry: date, spot: float, option_type: str, otm_target_abs_delta: float = 0.25) -> float | None:
    """Generic version of calculate_call_skew/calculate_put_skew for either side -- same underlying calculation, callable by option_type instead of two near-duplicate functions."""
    return (
        calculate_call_skew(underlying, expiry, spot, otm_target_abs_delta) if option_type == "CE"
        else calculate_put_skew(underlying, expiry, spot, otm_target_abs_delta)
    )


def detect_iv_expansion_or_compression(
    underlying: str, expiry: date, spot: float, lookback_snapshots: int = 5, threshold_pct: float = 15.0,
) -> dict:
    """
    Compares the CURRENT ATM IV against the average of the last
    `lookback_snapshots` ATM-IV readings for the SAME contract (walks
    apps.options.OptionChainSnapshot's own history, already stored by
    the 5-minute ingestion task -- no new data source). Returns
    {"direction": "expansion"|"compression"|"stable"|"unavailable",
    "current_iv", "baseline_iv", "change_pct"}.

    Uses the ATM CALL contract's own IV history specifically (not a
    recomputed "ATM IV" per snapshot, which could jump between
    different strikes as spot moves) -- so this tracks one real
    contract's IV path over time, the same way a trader watching one
    option's IV column move would.
    """
    from .models import OptionChainSnapshot, OptionContract

    calls = _contracts_with_greeks(underlying, expiry, "CE", spot)
    atm = _nearest_to_spot(calls, spot)
    if atm is None:
        return {"direction": "unavailable", "current_iv": None, "baseline_iv": None, "change_pct": None}

    history = list(
        OptionChainSnapshot.objects.filter(contract=atm["contract"], iv__isnull=False)
        .order_by("-timestamp")[: lookback_snapshots + 1]
    )
    if len(history) < 2:
        return {"direction": "unavailable", "current_iv": atm["iv"], "baseline_iv": None, "change_pct": None}

    current_iv = history[0].iv
    baseline_ivs = [s.iv for s in history[1:]]
    baseline_iv = sum(baseline_ivs) / len(baseline_ivs)
    if baseline_iv == 0:
        return {"direction": "unavailable", "current_iv": current_iv, "baseline_iv": baseline_iv, "change_pct": None}

    change_pct = round((current_iv - baseline_iv) / baseline_iv * 100, 2)
    if change_pct > threshold_pct:
        direction = "expansion"
    elif change_pct < -threshold_pct:
        direction = "compression"
    else:
        direction = "stable"
    return {"direction": direction, "current_iv": current_iv, "baseline_iv": round(baseline_iv, 4), "change_pct": change_pct}


def build_iv_term_structure(underlying: str) -> list[dict]:
    """
    ATM IV for every CURRENTLY SYNCED future expiry of this underlying,
    sorted nearest-first -- feasible now specifically because apps.
    options.tasks.sync_watchlist_option_contracts syncs SYNC_EXPIRY_COUNT
    (6) expiries per underlying, not just the nearest one. Each entry:
    {"expiry", "days_to_expiry", "atm_iv"}. An expiry with no scoreable
    ATM contract yet is simply omitted (not a fabricated 0), so the
    returned list can be shorter than the number of synced expiries.
    """
    from .models import OptionContract
    from .signals_engine import _latest_underlying_ltp

    expiries = list(
        OptionContract.objects.filter(underlying=underlying, expiry__gte=timezone.localdate())
        .order_by("expiry").values_list("expiry", flat=True).distinct()
    )
    today = timezone.localdate()
    term_structure = []
    for expiry in expiries:
        spot = _latest_underlying_ltp(underlying, expiry)
        if spot is None:
            continue
        atm = calculate_atm_iv(underlying, expiry, spot)
        if atm["average_iv"] is None:
            continue
        term_structure.append({
            "expiry": expiry, "days_to_expiry": (expiry - today).days, "atm_iv": atm["average_iv"],
        })
    return term_structure


def detect_contango_or_backwardation(term_structure: list[dict]) -> str:
    """
    "contango" = IV rises with time to expiry (near-term cheaper than
    far-term -- the normal/calm state). "backwardation" = near-term IV
    is elevated above far-term (typically event/expiry-specific
    near-term risk). Needs at least 2 expiries in the term structure;
    returns "insufficient_data" otherwise -- never guesses from one point.
    """
    if len(term_structure) < 2:
        return "insufficient_data"
    sorted_ts = sorted(term_structure, key=lambda t: t["days_to_expiry"])
    near, far = sorted_ts[0], sorted_ts[-1]
    if near["atm_iv"] < far["atm_iv"]:
        return "contango"
    if near["atm_iv"] > far["atm_iv"]:
        return "backwardation"
    return "flat"
