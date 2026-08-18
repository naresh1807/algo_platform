"""
"Best Strike" suggestion -- the manual's intro section states this
explicitly as one of the platform's core promises: given a directional
call (bullish/bearish), the system should analyze Probability (delta,
as a rough proxy -- see below), OI, Greeks, Volume, and Risk together
and suggest a strike, rather than leaving the user to manually compare
a full chain of strikes.

This is the one place in apps.options that pulls together metrics.py
(OI/PCR), greeks.py (delta/theta/vega via locally-solved IV), and
signals_engine.py's risk flags (IV crush, high-decay zone) into a
single ranked recommendation -- everything else in this app computes
ONE thing; this composes several into an actual suggestion, the same
role apps.signals.engine plays for the overall BUY/NO_TRADE decision.

Design choices (documented -- the manual states the requirement, not
an exact formula):
- "Probability" is approximated by the option's own delta -- for a
  bought option, |delta| is the standard rough proxy for probability
  of finishing in-the-money, no separate probability model needed.
- Candidates are restricted to strikes with delta in
  DELTA_SWEET_SPOT (0.35-0.65 by default) -- deep OTM (lottery-ticket,
  low probability) and deep ITM (expensive, low leverage) strikes are
  excluded before scoring rather than merely penalized, since neither
  is genuinely "the best strike" for a directional trade regardless of
  how much OI/volume it has.
- Within that band, strikes are scored on OI + volume (liquidity --
  normalized against the other candidates, not an absolute threshold)
  and penalized for weak theta-to-premium ratio (expensive decay
  relative to what's being paid). A strike already flagged by
  signals_engine's iv_crush/high_decay checks is excluded outright,
  not just penalized -- those are the same hard-veto conditions
  options_confluence_score treats as a veto for the composite signal
  engine, so a "best strike" suggestion should not recommend into one.
"""

from __future__ import annotations

from datetime import date

DELTA_SWEET_SPOT = (0.35, 0.65)  # |delta| range considered for suggestion


def _pick_by_moneyness(candidates_sorted: list[dict], spot: float, option_type: str, mode: str, steps: int) -> dict | None:
    """
    candidates_sorted: real synced-contract candidates, ascending by
    strike -- ATM/ITM/OTM only ever pick FROM this list (never invent a
    strike). ATM = nearest strike to spot. ITM/OTM step `steps` strikes
    further from ATM in the direction that's actually in-the-money/
    out-of-the-money for this option_type (CE: ITM is below spot, OTM is
    above; PE is the mirror image). If fewer than `steps` strikes exist
    in that direction, clamps to the furthest one actually available
    (still a real listed contract, just closer to ATM than requested)
    rather than returning nothing.
    """
    if not candidates_sorted:
        return None

    atm_idx = min(range(len(candidates_sorted)), key=lambda i: abs(candidates_sorted[i]["strike"] - spot))
    if mode == "atm":
        return candidates_sorted[atm_idx]

    stepping_down = (option_type == "CE" and mode == "itm") or (option_type == "PE" and mode == "otm")
    idx = atm_idx - steps if stepping_down else atm_idx + steps
    idx = max(0, min(idx, len(candidates_sorted) - 1))
    return candidates_sorted[idx]


def suggest_best_strike(underlying: str, expiry: date, direction: str, mode: str | None = None) -> dict:
    """
    direction: "bullish" (considers CE strikes) or "bearish" (considers PE strikes).
    mode: "ATM"/"ITM"/"OTM"/"AI" (case-insensitive), or None to read
    apps.options.models.OptionsStrategySetting.strike_mode -- defaults to
    AI, the original delta-sweet-spot + liquidity/decay scoring behavior
    below, fully unchanged for every existing caller that doesn't pass
    mode (e.g. apps.options.views.BestStrikeView). ATM/ITM/OTM instead
    pick the nearest actually-synced strike at the requested moneyness --
    see _pick_by_moneyness. All modes only ever choose from `candidates`
    below, which already requires a real snapshot + solvable IV for that
    contract, so no mode can ever suggest a strike without live data
    backing it.

    Returns {"suggested": {...} | None, "reason": str, "candidates": [...]}
    -- candidates lists every strike considered (even ones that lost),
    so a human/JARVIS can see the comparison, not just the winner
    (manual's "AI must explain every decision" principle applied here
    the same way apps.signals.engine explains every signal).
    """
    from . import metrics
    from .greeks import compute_greeks_for_contract
    from .models import OptionContract, get_options_strategy_settings
    from .signals_engine import _check_high_decay_zone, _check_iv_crush, _latest_underlying_ltp

    settings_row = None
    if mode is None:
        settings_row = get_options_strategy_settings()
        mode = settings_row.strike_mode
    mode = mode.lower()

    option_type = "CE" if direction == "bullish" else "PE"

    spot = _latest_underlying_ltp(underlying, expiry)
    if spot is None:
        return {"suggested": None, "reason": f"No underlying price data for {underlying} yet.", "candidates": []}

    if _check_high_decay_zone(expiry)["flag"]:
        return {
            "suggested": None,
            "reason": _check_high_decay_zone(expiry)["detail"],
            "candidates": [],
        }

    iv_crush = _check_iv_crush(underlying, expiry)

    contracts = OptionContract.objects.filter(underlying=underlying, expiry=expiry, option_type=option_type)
    # One query for every contract's latest snapshot (both option_type
    # sides -- metrics._latest_snapshots doesn't filter by side, but
    # only `contracts` above are iterated below) instead of a separate
    # query per contract, which used to make this loop 100+ round trips
    # for a real index expiry.
    latest_by_contract = {
        snap.contract_id: snap for snap in metrics._latest_snapshots(underlying, expiry)
    }
    candidates = []

    for contract in contracts:
        snapshot = latest_by_contract.get(contract.pk)
        if snapshot is None or snapshot.ltp is None:
            continue

        greeks = compute_greeks_for_contract(contract, spot, float(snapshot.ltp))
        if greeks is None:
            continue  # IV couldn't be solved for this strike -- skip rather than guess

        abs_delta = abs(greeks["delta"])
        candidates.append({
            "contract_id": contract.pk,
            "strike": float(contract.strike),
            "ltp": float(snapshot.ltp),
            "bid": float(snapshot.bid) if snapshot.bid is not None else None,
            "ask": float(snapshot.ask) if snapshot.ask is not None else None,
            "open_interest": snapshot.open_interest,
            "volume": snapshot.volume,
            "delta": greeks["delta"],
            "theta": greeks["theta"],
            "vega": greeks["vega"],
            "iv": greeks["iv"],
            "in_sweet_spot": DELTA_SWEET_SPOT[0] <= abs_delta <= DELTA_SWEET_SPOT[1],
        })

    if not candidates:
        return {"suggested": None, "reason": "No scoreable contracts (missing snapshots or IV couldn't be solved).", "candidates": []}

    if mode == "ai":
        eligible = [c for c in candidates if c["in_sweet_spot"]]
        if not eligible:
            return {
                "suggested": None,
                "reason": f"No strike has delta in the {DELTA_SWEET_SPOT} sweet spot right now.",
                "candidates": candidates,
            }

        max_oi = max(c["open_interest"] for c in eligible) or 1
        max_volume = max(c["volume"] for c in eligible) or 1

        for c in eligible:
            liquidity_score = 0.6 * (c["open_interest"] / max_oi) + 0.4 * (c["volume"] / max_volume)
            # Theta-to-premium: how much of the option's own price decays
            # per day, as a fraction -- lower is better (less of what you
            # paid evaporates from time alone). abs() since theta is
            # negative for a long option.
            theta_drag = abs(c["theta"]) / c["ltp"] if c["ltp"] else 1.0
            decay_score = max(0.0, 1.0 - theta_drag * 10)  # scaled so a few % daily decay meaningfully penalizes
            c["score"] = round(0.6 * liquidity_score + 0.4 * decay_score, 4)

        eligible.sort(key=lambda c: c["score"], reverse=True)
        best = eligible[0]
        reason_parts = [
            f"AI-selected {best['strike']} {option_type}: delta {best['delta']:+.2f} (probability proxy), "
            f"OI {best['open_interest']:,}, volume {best['volume']:,}, IV {best['iv']}%, "
            f"theta {best['theta']:+.2f}/day.",
        ]
    elif mode in ("atm", "itm", "otm"):
        steps = settings_row.itm_otm_steps if settings_row is not None else get_options_strategy_settings().itm_otm_steps
        candidates_sorted = sorted(candidates, key=lambda c: c["strike"])
        best = _pick_by_moneyness(candidates_sorted, spot, option_type, mode, steps)
        if best is None:
            return {"suggested": None, "reason": f"No synced strikes available for {mode.upper()} mode.", "candidates": candidates}
        reason_parts = [
            f"{mode.upper()} mode: {best['strike']} {option_type} (spot {spot}), "
            f"delta {best['delta']:+.2f}, OI {best['open_interest']:,}, volume {best['volume']:,}, IV {best['iv']}%.",
        ]
    else:
        return {"suggested": None, "reason": f"Unknown strike_mode {mode!r}.", "candidates": candidates}

    if iv_crush["flag"]:
        reason_parts.append(f"Note: {iv_crush['detail']}")

    return {"suggested": best, "reason": " ".join(reason_parts), "candidates": candidates}
