"""
OI Intelligence: extends apps.options.signals_engine's existing
per-side buildup/unwinding classification (_aggregate_side_signal --
OI-weighted price+OI change combination, already NOT the "OI change
alone" shortcut this platform is explicitly not supposed to use) with
volume corroboration (confidence scoring), OI concentration, and
OI-migration tracking across time -- reusing that module's private
snapshot-fetching helpers rather than re-querying the chain a second
way.

Also home to institutional_positioning_proxy(): explicitly labeled
"PROXY" in its own output, never claiming real institutional/dealer
data (no such data source exists anywhere in this platform -- see
apps.options.exposure's identical caveat for gamma/vanna/charm).
"""

from __future__ import annotations

from datetime import date


def classify_buildup_with_confidence(underlying: str, expiry: date, option_type: str) -> dict:
    """
    Same OI+price weighted classification as apps.options.
    signals_engine._aggregate_side_signal, plus a confidence score from
    volume corroboration: elevated volume alongside an OI/price move is
    the evidence that separates a real repositioning from thin, easily
    -reversed activity that happens to tick OI/price the same way.

    Returns {"classification": str | None, "confidence": float | None,
    "volume_ratio": float | None, "detail": str}. confidence is
    deliberately capped in [0.3, 0.95] -- never absolute certainty from
    OI/volume/price alone, and never exactly 0 either (an OI+price
    signal without volume corroboration is still SOME evidence, not
    none).
    """
    from .models import OptionContract
    from .signals_engine import _aggregate_side_signal, _recent_snapshots_by_contract

    classification = _aggregate_side_signal(underlying, expiry, option_type)
    if classification is None:
        return {"classification": None, "confidence": None, "volume_ratio": None, "detail": "Not enough snapshot history yet."}

    contracts = list(OptionContract.objects.filter(underlying=underlying, expiry=expiry, option_type=option_type))
    # 3 snapshots per contract: [0]=latest (the move being classified),
    # [1:]=the recent baseline volume is compared against.
    recent_by_contract = _recent_snapshots_by_contract(contracts, 3)

    total_latest_volume = 0
    total_baseline_volume = 0.0
    contracts_with_baseline = 0
    for contract in contracts:
        recent = recent_by_contract.get(contract.pk, [])
        if len(recent) < 2:
            continue
        total_latest_volume += recent[0].volume or 0
        baseline = recent[1:]
        if baseline:
            total_baseline_volume += sum(s.volume or 0 for s in baseline) / len(baseline)
            contracts_with_baseline += 1

    if contracts_with_baseline == 0 or total_baseline_volume == 0:
        return {
            "classification": classification, "confidence": 0.5, "volume_ratio": None,
            "detail": f"{classification} on {option_type} side (no volume baseline available yet -- moderate confidence).",
        }

    volume_ratio = total_latest_volume / total_baseline_volume
    if volume_ratio >= 1.3:
        confidence = 0.95
    elif volume_ratio <= 0.7:
        confidence = 0.3
    else:
        confidence = 0.3 + (volume_ratio - 0.7) / 0.6 * 0.65  # linear: 0.7->0.3, 1.3->0.95

    return {
        "classification": classification, "confidence": round(confidence, 2), "volume_ratio": round(volume_ratio, 2),
        "detail": (
            f"{classification} on {option_type} side, volume {volume_ratio:.2f}x the recent baseline "
            f"({'corroborating' if volume_ratio >= 1.3 else 'undercutting' if volume_ratio <= 0.7 else 'partially corroborating'})."
        ),
    }


def calculate_oi_concentration(underlying: str, expiry: date, option_type: str, top_n: int = 3) -> dict:
    """
    What fraction of TOTAL OI on one side sits in the top_n strikes --
    a different question than "which strike has the most OI" (apps.
    options.metrics.strike_support_resistance already answers that).
    High concentration means positioning is stacked at a few strikes
    (a real wall); low concentration means OI is spread thin across the
    chain (no single strike is especially meaningful).
    """
    from . import metrics

    snapshots = metrics._latest_snapshots(underlying, expiry)
    side_snapshots = [s for s in snapshots if s.contract.option_type == option_type]
    if not side_snapshots:
        return {"concentration_pct": None, "top_strikes": []}

    total_oi = sum(s.open_interest for s in side_snapshots)
    if total_oi == 0:
        return {"concentration_pct": None, "top_strikes": []}

    top = sorted(side_snapshots, key=lambda s: s.open_interest, reverse=True)[:top_n]
    top_oi = sum(s.open_interest for s in top)
    return {
        "concentration_pct": round(top_oi / total_oi * 100, 2),
        "top_strikes": [{"strike": float(s.contract.strike), "oi": s.open_interest} for s in top],
    }


def detect_oi_migration(underlying: str, expiry: date, option_type: str, lookback_points: int = 3) -> dict:
    """
    Tracks which strike held the MAX OI on this side across the last
    `lookback_points` distinct ingestion timestamps -- reports whether
    that peak has genuinely MOVED (e.g. 24500 -> 24550 -> 24600, real
    repositioning) rather than treating each snapshot as an isolated
    reading, per this module's own instruction to interpret OI shifts
    as a migration event, not a series of unrelated point-in-time facts.
    """
    from .models import OptionChainSnapshot

    distinct_timestamps = list(
        OptionChainSnapshot.objects.filter(
            contract__underlying=underlying, contract__expiry=expiry, contract__option_type=option_type,
        ).order_by("-timestamp").values_list("timestamp", flat=True).distinct()[:lookback_points]
    )
    if len(distinct_timestamps) < 2:
        return {"migrated": False, "peak_strike_sequence": [], "detail": "Not enough historical snapshots yet."}

    peak_sequence = []
    for ts in reversed(distinct_timestamps):  # oldest first, so the sequence reads chronologically
        top = (
            OptionChainSnapshot.objects.filter(
                contract__underlying=underlying, contract__expiry=expiry,
                contract__option_type=option_type, timestamp=ts,
            ).select_related("contract").order_by("-open_interest").first()
        )
        if top is not None:
            peak_sequence.append(float(top.contract.strike))

    migrated = len(set(peak_sequence)) > 1
    if migrated:
        detail = f"OI concentration peak moved: {' -> '.join(str(s) for s in peak_sequence)}."
    elif peak_sequence:
        detail = f"OI concentration peak has held at {peak_sequence[-1]}."
    else:
        detail = "No data."
    return {"migrated": migrated, "peak_strike_sequence": peak_sequence, "detail": detail}


def institutional_positioning_proxy(underlying: str, expiry: date, direction: str) -> dict:
    """
    "Smart money proxy" -- NEVER claims real institutional/dealer data
    (no such data source exists). Combines classify_buildup_with_
    confidence + calculate_oi_concentration on the option side matching
    `direction` ("bullish"->CE, "bearish"->PE) into one leaning read.
    Always returns "label": "PROXY" so this can never be silently
    mistaken for verified positioning data downstream.
    """
    option_type = "CE" if direction == "bullish" else "PE"
    buildup = classify_buildup_with_confidence(underlying, expiry, option_type)
    concentration = calculate_oi_concentration(underlying, expiry, option_type)

    if buildup["classification"] is None:
        return {
            "label": "PROXY", "leaning": "insufficient_data", "confidence": None,
            "oi_concentration_pct": concentration["concentration_pct"],
            "detail": "Not enough option-chain history yet to form a positioning proxy.",
        }

    accumulating_classifications = {"buildup_bullish", "short_covering"}
    leaning = (
        "large_holders_likely_accumulating" if buildup["classification"] in accumulating_classifications
        else "large_holders_likely_distributing"
    )

    return {
        "label": "PROXY",
        "leaning": leaning,
        "confidence": buildup["confidence"],
        "oi_concentration_pct": concentration["concentration_pct"],
        "detail": (
            f"Chain-derived PROXY (not real institutional data): {buildup['detail']} "
            f"{concentration['concentration_pct']}% of {option_type} OI concentrated in the top strikes."
            if concentration["concentration_pct"] is not None else
            f"Chain-derived PROXY (not real institutional data): {buildup['detail']}"
        ),
    }
