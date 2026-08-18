"""
Data Quality Engine: "never generate a trade signal using invalid or
stale data." Sits BEFORE any scoring/selection logic -- every function
here is a pure `(ok: bool, reason: str)` check over plain values (same
convention as apps.risk.engine's checks and apps.options.strike_selector's
candidate dicts), so nothing here needs an ORM object, and every check is
independently unit-testable against synthetic fixtures.

Two tiers:
  1. Per-field validators (validate_ltp/bid_ask/oi/volume/iv/greeks/
     expiry/strike, detect_bad_ticks/stale_quotes/zero_volume/
     wide_spread) -- used ad hoc by any caller that has one contract's
     worth of data in hand (e.g. a REST view sanity-checking a quote
     before displaying it).
  2. validate_option_chain_snapshot() -- the chain-wide gate apps.options.
     index_direction_strategy actually calls before it will even attempt
     direction/strike analysis for an underlying+expiry. Deliberately
     does NOT require every one of a chain's 40+ strikes to be perfect
     (a handful of zero-volume deep-OTM strikes is normal, not a feed
     failure) -- it checks whether the ingestion pipeline for THIS
     underlying+expiry is actually alive (at least one recent, sane
     snapshot exists), which is the thing that actually matters before
     trusting anything downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

# Matches greeks.py's implied_volatility bisection bounds (sigma in
# [1e-5, 5.0]) expressed as a percentage -- an IV reading outside this
# range didn't come from our own solver and isn't a plausible broker
# quote either, so it's treated as a bad tick rather than a real
# (if extreme) volatility reading.
MIN_SANE_IV_PCT = 0.001
MAX_SANE_IV_PCT = 500.0

# Same threshold apps.risk.engine.FEED_STALENESS_THRESHOLD_MINUTES uses
# for the underlying candle feed -- kept as an independent constant
# (not imported from there) since apps.risk lazy-importing apps.options
# already happens one direction (_is_options_expiry_day); importing
# back the other way for a single constant isn't worth the coupling.
DEFAULT_STALENESS_THRESHOLD_MINUTES = 15

# A live LTP more than this fraction outside its own [bid, ask] band is
# almost certainly a bad tick (a crossed/stale quote, or a print from
# before the spread moved) rather than a real trade -- 20% is
# deliberately generous (illiquid index-option legs can have wide
# spreads and a last trade can lag the current quote by a few strikes'
# worth of premium during a fast move), not a tight arbitrage-free bound.
BAD_TICK_LTP_TOLERANCE_PCT = 20.0


def validate_underlying_data(spot: float | None) -> tuple[bool, str]:
    if spot is None:
        return False, "Underlying spot price is missing."
    if spot <= 0:
        return False, f"Underlying spot price {spot} is not positive."
    return True, ""


def validate_expiry(expiry, today=None) -> tuple[bool, str]:
    today = today or timezone.localdate()
    if expiry is None:
        return False, "Expiry is missing."
    if expiry < today:
        return False, f"Expiry {expiry} has already passed."
    return True, ""


def validate_strike(strike: float | None) -> tuple[bool, str]:
    if strike is None:
        return False, "Strike is missing."
    if strike <= 0:
        return False, f"Strike {strike} is not positive."
    return True, ""


def validate_ltp(ltp: float | None) -> tuple[bool, str]:
    if ltp is None:
        return False, "LTP is missing."
    if ltp <= 0:
        return False, f"LTP {ltp} is not positive."
    return True, ""


def validate_bid_ask(bid: float | None, ask: float | None) -> tuple[bool, str]:
    if bid is None or ask is None:
        return False, "Bid/ask is missing."
    if bid < 0 or ask <= 0:
        return False, f"Bid/ask ({bid}, {ask}) has a non-positive ask or negative bid."
    if bid > ask:
        return False, f"Bid ({bid}) is greater than ask ({ask}) -- crossed/bad quote."
    return True, ""


def validate_oi(open_interest: int | None) -> tuple[bool, str]:
    if open_interest is None:
        return False, "Open interest is missing."
    if open_interest < 0:
        return False, f"Open interest {open_interest} is negative."
    return True, ""


def validate_volume(volume: int | None) -> tuple[bool, str]:
    if volume is None:
        return False, "Volume is missing."
    if volume < 0:
        return False, f"Volume {volume} is negative."
    return True, ""


def validate_iv(iv: float | None) -> tuple[bool, str]:
    if iv is None:
        return False, "IV is missing."
    if not (MIN_SANE_IV_PCT <= iv <= MAX_SANE_IV_PCT):
        return False, f"IV {iv}% is outside the sane range [{MIN_SANE_IV_PCT}, {MAX_SANE_IV_PCT}]."
    return True, ""


def validate_greeks(greeks: dict | None) -> tuple[bool, str]:
    if not greeks:
        return False, "Greeks are missing."
    delta = greeks.get("delta")
    gamma = greeks.get("gamma")
    vega = greeks.get("vega")
    if delta is None or not (-1.0 <= delta <= 1.0):
        return False, f"Delta {delta} is outside [-1, 1]."
    if gamma is not None and gamma < 0:
        return False, f"Gamma {gamma} is negative (gamma is never negative for a vanilla option)."
    if vega is not None and vega < 0:
        return False, f"Vega {vega} is negative (vega is never negative for a vanilla option)."
    return True, ""


def detect_stale_quotes(
    quote_timestamp: datetime | None, now: datetime | None = None,
    threshold_minutes: int = DEFAULT_STALENESS_THRESHOLD_MINUTES,
) -> tuple[bool, str]:
    """Returns (is_stale, reason) -- True means the quote IS stale (a flag, not a pass/fail "ok")."""
    if quote_timestamp is None:
        return True, "No quote timestamp -- treated as stale."
    now = now or timezone.now()
    age = now - quote_timestamp
    if age > timedelta(minutes=threshold_minutes):
        return True, f"Quote is {age.total_seconds() / 60:.1f} min old (> {threshold_minutes}min threshold)."
    return False, ""


def detect_missing_quotes(snapshot) -> tuple[bool, str]:
    """snapshot: an OptionChainSnapshot instance or None. Returns (is_missing, reason)."""
    if snapshot is None:
        return True, "No snapshot exists for this contract."
    return False, ""


def detect_bad_ticks(ltp: float | None, bid: float | None, ask: float | None) -> tuple[bool, str]:
    """Returns (is_bad_tick, reason) -- True means a bad tick WAS detected."""
    if ltp is None or bid is None or ask is None:
        return False, ""  # missing data is its own separate check, not a "bad tick"
    if bid > ask:
        return True, f"Crossed quote: bid {bid} > ask {ask}."
    mid = (bid + ask) / 2
    if mid <= 0:
        return True, f"Non-positive bid/ask midpoint ({mid})."
    deviation_pct = abs(ltp - mid) / mid * 100
    if deviation_pct > BAD_TICK_LTP_TOLERANCE_PCT:
        return True, f"LTP {ltp} is {deviation_pct:.1f}% away from the bid/ask midpoint {mid} (> {BAD_TICK_LTP_TOLERANCE_PCT}% tolerance)."
    return False, ""


def detect_zero_volume_contracts(volume: int | None) -> tuple[bool, str]:
    """
    Informational, not a hard invalidity -- a deep-OTM strike genuinely
    trading zero volume in a given window is normal, not a feed error.
    Callers that care about tradeability should use liquidity checks
    (apps.risk.engine.check_option_contract_liquidity), not this alone.
    """
    if volume is not None and volume == 0:
        return True, "Zero volume -- illiquid or untraded this window."
    return False, ""


def detect_wide_spread_contracts(bid: float | None, ask: float | None, max_spread_pct: float = 10.0) -> tuple[bool, str]:
    if bid is None or ask is None or bid <= 0:
        return False, ""
    mid = (bid + ask) / 2
    if mid <= 0:
        return False, ""
    spread_pct = (ask - bid) / mid * 100
    if spread_pct > max_spread_pct:
        return True, f"Bid/ask spread {spread_pct:.1f}% exceeds {max_spread_pct}% reference threshold."
    return False, ""


@dataclass
class DataQualityReport:
    valid: bool
    status: str  # "DATA_VALID" | "DATA_INVALID"
    issues: list[str] = field(default_factory=list)


def validate_option_chain_snapshot(
    underlying: str, expiry, staleness_threshold_minutes: int = DEFAULT_STALENESS_THRESHOLD_MINUTES,
) -> DataQualityReport:
    """
    The chain-wide gate apps.options.index_direction_strategy calls
    before attempting any direction/strike analysis for this underlying
    +expiry. Passes as long as the ingestion pipeline for this chain is
    demonstrably alive: at least one contract has a snapshot, and at
    least one of those snapshots is fresh (within staleness_threshold_
    minutes) with sane LTP/bid-ask. Does NOT require every strike to be
    perfect -- a chain full of mostly-illiquid far-OTM strikes is
    normal; a chain where every snapshot is hours old or missing means
    the broker feed/Celery ingestion has stopped, which is what this
    actually guards against.
    """
    from .metrics import _latest_snapshots
    from .models import OptionContract

    issues: list[str] = []

    expiry_ok, expiry_reason = validate_expiry(expiry)
    if not expiry_ok:
        return DataQualityReport(valid=False, status="DATA_INVALID", issues=[expiry_reason])

    contracts = list(OptionContract.objects.filter(underlying=underlying, expiry=expiry))
    if not contracts:
        return DataQualityReport(
            valid=False, status="DATA_INVALID",
            issues=[f"No OptionContract rows synced for {underlying} {expiry}."],
        )

    snapshots = list(_latest_snapshots(underlying, expiry))
    if not snapshots:
        return DataQualityReport(
            valid=False, status="DATA_INVALID",
            issues=[f"No OptionChainSnapshot rows for {underlying} {expiry} -- ingestion hasn't run yet."],
        )

    now = timezone.now()
    fresh_and_sane_count = 0
    for snapshot in snapshots:
        is_stale, _ = detect_stale_quotes(snapshot.timestamp, now, staleness_threshold_minutes)
        if is_stale:
            continue
        ltp_ok, _ = validate_ltp(float(snapshot.ltp) if snapshot.ltp is not None else None)
        if not ltp_ok:
            continue
        fresh_and_sane_count += 1

    if fresh_and_sane_count == 0:
        issues.append(
            f"Every snapshot for {underlying} {expiry} is either stale (> "
            f"{staleness_threshold_minutes}min old) or has an invalid LTP -- the option-chain "
            f"ingestion pipeline may have stopped."
        )
        return DataQualityReport(valid=False, status="DATA_INVALID", issues=issues)

    return DataQualityReport(valid=True, status="DATA_VALID", issues=issues)
