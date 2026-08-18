"""
Anomaly Detection: z-score of a current reading against a ROLLING
HISTORICAL BASELINE built from apps.options.OptionChainSnapshot's own
history (already ingested every 5 minutes) -- never an arbitrary fixed
threshold, per this platform's own instruction to use historical
baselines rather than hand-picked cutoffs.

Bid/ask SIZE imbalance is explicitly UNAVAILABLE here: OptionChainSnapshot
stores bid/ask PRICE only, never quantity/depth, and no market-depth
data source exists anywhere in this codebase. detect_bid_ask_imbalance_
anomaly stubs this honestly rather than computing a number from data
that doesn't exist.
"""

from __future__ import annotations

import math

# ~95% of a normal distribution falls within +/-2 standard deviations
# -- a standard, not arbitrary, statistical convention for "unusual".
DEFAULT_Z_SCORE_THRESHOLD = 2.0

DEFAULT_LOOKBACK = 10
MIN_BASELINE_SIZE = 5  # fewer than this and a z-score is mostly noise, not a real baseline


def _z_score_anomaly(current: float | None, baseline: list[float], threshold: float = DEFAULT_Z_SCORE_THRESHOLD) -> dict:
    """Pure statistics -- no DB access. Returns {"is_anomaly", "z_score", "detail"}."""
    if current is None or len(baseline) < MIN_BASELINE_SIZE:
        return {"is_anomaly": False, "z_score": None, "detail": f"Insufficient history (need >= {MIN_BASELINE_SIZE} baseline readings)."}

    mean = sum(baseline) / len(baseline)
    variance = sum((b - mean) ** 2 for b in baseline) / len(baseline)
    std = math.sqrt(variance)
    if std == 0:
        return {"is_anomaly": False, "z_score": None, "detail": "Baseline has zero variance -- every reading was identical."}

    z = (current - mean) / std
    return {
        "is_anomaly": abs(z) > threshold, "z_score": round(z, 2),
        "detail": f"z={z:.2f} (threshold {threshold}), baseline mean={mean:.2f}, std={std:.2f} over {len(baseline)} readings.",
    }


def _snapshot_history(contract, lookback: int) -> list:
    from .models import OptionChainSnapshot

    return list(
        OptionChainSnapshot.objects.filter(contract=contract).order_by("-timestamp")[: lookback + 1]
    )


def detect_volume_anomaly(contract, lookback: int = DEFAULT_LOOKBACK) -> dict:
    """Current snapshot's volume vs. this contract's own recent volume history."""
    history = _snapshot_history(contract, lookback)
    if len(history) < 2:
        return _z_score_anomaly(None, [])
    current = history[0].volume
    baseline = [s.volume for s in history[1:] if s.volume is not None]
    return _z_score_anomaly(current, baseline)


def detect_oi_change_anomaly(contract, lookback: int = DEFAULT_LOOKBACK) -> dict:
    """Current snapshot's change_in_oi vs. this contract's own recent OI-change history."""
    history = _snapshot_history(contract, lookback)
    if len(history) < 2:
        return _z_score_anomaly(None, [])
    current = history[0].change_in_oi
    baseline = [s.change_in_oi for s in history[1:] if s.change_in_oi is not None]
    return _z_score_anomaly(current, baseline)


def detect_iv_anomaly(contract, lookback: int = DEFAULT_LOOKBACK) -> dict:
    """Current snapshot's IV vs. this contract's own recent IV history."""
    history = _snapshot_history(contract, lookback)
    if len(history) < 2:
        return _z_score_anomaly(None, [])
    current = history[0].iv
    baseline = [s.iv for s in history[1:] if s.iv is not None]
    return _z_score_anomaly(current, baseline)


def detect_premium_anomaly(contract, lookback: int = DEFAULT_LOOKBACK) -> dict:
    """
    Current snapshot's PERIOD-OVER-PERIOD premium % change vs. this
    contract's own recent history of such changes -- deliberately a
    change series, not raw LTP levels (a contract's premium drifting
    steadily as it approaches expiry is normal theta decay, not an
    anomaly; a sudden jump in the SIZE of period-over-period moves is
    what actually signals something unusual happened).
    """
    history = _snapshot_history(contract, lookback + 1)
    if len(history) < 3:
        return _z_score_anomaly(None, [])

    changes = []
    for i in range(len(history) - 1):
        newer, older = history[i], history[i + 1]
        if older.ltp and older.ltp > 0:
            changes.append(float((newer.ltp - older.ltp) / older.ltp) * 100)
    if len(changes) < 2:
        return _z_score_anomaly(None, [])

    current = changes[0]
    baseline = changes[1:]
    return _z_score_anomaly(current, baseline)


def detect_bid_ask_imbalance_anomaly(*args, **kwargs) -> dict:
    """
    Explicitly UNAVAILABLE -- see module docstring. Kept as a callable
    stub (not simply absent) so a caller iterating "every anomaly type"
    gets an honest, typed answer instead of an AttributeError.
    """
    return {
        "available": False,
        "reason": "Bid/ask SIZE (quantity/depth), not just price, is required for a real imbalance "
                   "calculation -- this platform's OptionChainSnapshot stores bid/ask price only, "
                   "and no market-depth data source exists.",
    }
