"""
Expected Move Engine -- the standard 1-standard-deviation options
expected-move formula (spot * IV * sqrt(time-to-expiry-in-years)),
applied to this platform's own already-solved ATM IV (apps.options.
volatility_surface.calculate_atm_iv) rather than a fabricated or
externally-fetched volatility number.
"""

from __future__ import annotations

import math
from datetime import date

from django.utils import timezone


def calculate_expected_move(spot: float | None, iv_pct: float | None, days_to_expiry: int | None) -> dict:
    """
    Pure function: expected_move = spot * (iv_pct/100) * sqrt(days_to_expiry/365).
    Returns {"expected_move", "upper_range", "lower_range", "days_to_expiry"},
    all None if any required input is missing/invalid -- never a
    fabricated 0.
    """
    if spot is None or iv_pct is None or days_to_expiry is None or spot <= 0 or iv_pct <= 0 or days_to_expiry < 0:
        return {"expected_move": None, "upper_range": None, "lower_range": None, "days_to_expiry": days_to_expiry}

    sigma_decimal = iv_pct / 100.0
    expected_move = spot * sigma_decimal * math.sqrt(days_to_expiry / 365.0)
    return {
        "expected_move": round(expected_move, 2),
        "upper_range": round(spot + expected_move, 2),
        "lower_range": round(spot - expected_move, 2),
        "days_to_expiry": days_to_expiry,
    }


def calculate_expected_move_for_contract(underlying: str, expiry: date, spot: float) -> dict:
    """
    Convenience wrapper for the common call shape: resolves ATM IV
    (apps.options.volatility_surface.calculate_atm_iv, averaging both
    CE/PE sides when available) and days-to-expiry itself, then calls
    calculate_expected_move above. Adds "atm_iv" to the returned dict
    so a caller can see exactly which IV reading the move was derived
    from.
    """
    from .volatility_surface import calculate_atm_iv

    atm = calculate_atm_iv(underlying, expiry, spot)
    days_to_expiry = (expiry - timezone.localdate()).days
    result = calculate_expected_move(spot, atm["average_iv"], days_to_expiry)
    result["atm_iv"] = atm["average_iv"]
    return result


def classify_price_vs_expected_range(
    spot: float | None, upper_range: float | None, lower_range: float | None, near_threshold_pct: float = 10.0,
) -> str:
    """
    Where the CURRENT spot sits relative to an already-computed expected
    range: "inside_range" | "near_upper_range" | "near_lower_range" |
    "outside_upper_range" | "outside_lower_range" | "unavailable".

    "near" means within near_threshold_pct of the range's own WIDTH
    from either boundary (not an absolute price distance), so the
    threshold scales naturally with how wide the expected move is for
    a given IV/time-to-expiry instead of needing a separate hand-tuned
    absolute-price constant.
    """
    if spot is None or upper_range is None or lower_range is None:
        return "unavailable"
    if spot > upper_range:
        return "outside_upper_range"
    if spot < lower_range:
        return "outside_lower_range"

    width = upper_range - lower_range
    if width <= 0:
        return "unavailable"

    distance_to_upper_pct = (upper_range - spot) / width * 100
    distance_to_lower_pct = (spot - lower_range) / width * 100
    if distance_to_upper_pct <= near_threshold_pct:
        return "near_upper_range"
    if distance_to_lower_pct <= near_threshold_pct:
        return "near_lower_range"
    return "inside_range"
