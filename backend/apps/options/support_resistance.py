"""
Dynamic Support/Resistance Engine -- combines several INDEPENDENT
inputs (OI walls, VWAP/VWAP bands, prior day's structure, expected-move
boundaries) into confluence zones, instead of the "highest OI = the
support level" shortcut the platform is explicitly not supposed to use.
A level backed by only one source is reported as such; a level where
multiple independent sources agree (within a small tolerance) is
reported with a higher confluence_count -- that count, not any single
source, is what should carry weight downstream.
"""

from __future__ import annotations

from datetime import date

from django.utils import timezone

# Levels within this % of each other are treated as "the same zone" --
# small enough that two genuinely different strikes/levels don't get
# merged, large enough that e.g. a VWAP reading and a nearby put-OI
# wall a few points apart are recognized as reinforcing each other.
DEFAULT_CLUSTER_TOLERANCE_PCT = 0.3

# How far price must move past a zone's level to count as a real break,
# not just noise sitting exactly on the line.
DEFAULT_BREAK_TOLERANCE_PCT = 0.1


def _previous_day_ohlc(underlying: str) -> dict | None:
    from apps.market_data.models import HistoricalData

    today = timezone.localdate()
    candle = (
        HistoricalData.objects.filter(symbol=underlying, timeframe="1d", timestamp__date__lt=today)
        .order_by("-timestamp").first()
    )
    if candle is None:
        return None
    return {"high": float(candle.high), "low": float(candle.low), "close": float(candle.close)}


def _cluster_levels(candidates: list[dict], tolerance_pct: float = DEFAULT_CLUSTER_TOLERANCE_PCT) -> list[dict]:
    """
    Merges nearby levels from possibly-different sources into confluence
    zones. Each input candidate: {"level": float, "source": str,
    "detail": str}. Output: [{"level": avg, "sources": [...],
    "confluence_count": int, "details": [...]}], sorted nearest-level-first
    is the CALLER's job (this only clusters, doesn't sort by spot proximity).
    """
    if not candidates:
        return []
    sorted_candidates = sorted(candidates, key=lambda c: c["level"])
    clusters = [[sorted_candidates[0]]]
    for c in sorted_candidates[1:]:
        ref = clusters[-1][-1]["level"]
        if ref != 0 and abs(c["level"] - ref) / abs(ref) * 100 <= tolerance_pct:
            clusters[-1].append(c)
        else:
            clusters.append([c])

    result = []
    for cluster in clusters:
        avg_level = sum(c["level"] for c in cluster) / len(cluster)
        result.append({
            "level": round(avg_level, 2),
            "sources": [c["source"] for c in cluster],
            "confluence_count": len({c["source"] for c in cluster}),
            "details": [c["detail"] for c in cluster],
        })
    return result


def _candidate_levels(underlying: str, expiry: date, spot: float, timeframe: str = "5m") -> list[dict]:
    from . import metrics
    from .expected_move import calculate_expected_move_for_contract
    from apps.market_data.vwap import calculate_vwap_with_bands

    candidates: list[dict] = []

    sr = metrics.strike_support_resistance(underlying, expiry)
    for s in sr["support"]:
        candidates.append({"level": s["strike"], "source": "oi_put_wall", "detail": f"Put OI {s['oi']:,} at {s['strike']}"})
    for s in sr["resistance"]:
        candidates.append({"level": s["strike"], "source": "oi_call_wall", "detail": f"Call OI {s['oi']:,} at {s['strike']}"})

    vwap_result = calculate_vwap_with_bands(underlying, timeframe)
    if vwap_result["vwap"] is not None:
        candidates.append({"level": vwap_result["vwap"], "source": "vwap", "detail": f"VWAP {vwap_result['vwap']}"})
    if vwap_result["upper_band"] is not None:
        candidates.append({"level": vwap_result["upper_band"], "source": "vwap_upper_band", "detail": "VWAP + 1 std dev"})
    if vwap_result["lower_band"] is not None:
        candidates.append({"level": vwap_result["lower_band"], "source": "vwap_lower_band", "detail": "VWAP - 1 std dev"})

    prev = _previous_day_ohlc(underlying)
    if prev is not None:
        candidates.append({"level": prev["low"], "source": "prior_day_low", "detail": f"Prior day low {prev['low']}"})
        candidates.append({"level": prev["high"], "source": "prior_day_high", "detail": f"Prior day high {prev['high']}"})
        candidates.append({"level": prev["close"], "source": "prior_day_close", "detail": f"Prior day close {prev['close']}"})

    em = calculate_expected_move_for_contract(underlying, expiry, spot)
    if em["lower_range"] is not None:
        candidates.append({"level": em["lower_range"], "source": "expected_move_lower", "detail": f"Expected-move lower bound {em['lower_range']}"})
    if em["upper_range"] is not None:
        candidates.append({"level": em["upper_range"], "source": "expected_move_upper", "detail": f"Expected-move upper bound {em['upper_range']}"})

    return candidates


def calculate_dynamic_support(underlying: str, expiry: date, spot: float, timeframe: str = "5m") -> list[dict]:
    """
    Every candidate level BELOW spot, clustered into confluence zones,
    nearest-to-spot first. Deliberately does not rely exclusively on
    OI (per this module's own docstring) -- a zone backed only by
    "oi_put_wall" is reported as such, not elevated above one backed by
    multiple independent sources.
    """
    candidates = [c for c in _candidate_levels(underlying, expiry, spot, timeframe) if c["level"] < spot]
    zones = _cluster_levels(candidates)
    zones.sort(key=lambda z: spot - z["level"])
    return zones


def calculate_dynamic_resistance(underlying: str, expiry: date, spot: float, timeframe: str = "5m") -> list[dict]:
    """Mirror of calculate_dynamic_support for levels ABOVE spot."""
    candidates = [c for c in _candidate_levels(underlying, expiry, spot, timeframe) if c["level"] > spot]
    zones = _cluster_levels(candidates)
    zones.sort(key=lambda z: z["level"] - spot)
    return zones


def detect_support_break(spot: float, support_zones: list[dict], tolerance_pct: float = DEFAULT_BREAK_TOLERANCE_PCT) -> bool:
    """True if spot has moved below the nearest support zone by more than tolerance_pct (a real break, not noise on the line)."""
    if not support_zones:
        return False
    nearest = support_zones[0]["level"]
    if nearest == 0:
        return False
    return (nearest - spot) / abs(nearest) * 100 > tolerance_pct


def detect_resistance_break(spot: float, resistance_zones: list[dict], tolerance_pct: float = DEFAULT_BREAK_TOLERANCE_PCT) -> bool:
    """True if spot has moved above the nearest resistance zone by more than tolerance_pct."""
    if not resistance_zones:
        return False
    nearest = resistance_zones[0]["level"]
    if nearest == 0:
        return False
    return (spot - nearest) / abs(nearest) * 100 > tolerance_pct
