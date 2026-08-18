"""
manual section 9's "Options signals" list. Each function here compares
a contract's LATEST snapshot against its PREVIOUS one (OI + price
direction together) -- this is the standard options-flow reading
convention: OI change alone is ambiguous (rising OI could mean fresh
buying or fresh selling), so every signal here looks at price movement
alongside OI change to disambiguate:

  Price up  + OI up   -> long buildup (bullish)
  Price down+ OI up   -> short buildup (bearish)
  Price up  + OI down -> short covering (bullish)
  Price down+ OI down -> long unwinding (bearish)

This module reasons about aggregate CALL-side and PUT-side OI/price
behavior (not per-strike) to produce the underlying-level signals the
manual lists (bullish/bearish call buildup, short-covering, long
unwinding) -- per-strike buildup is available via
apps.options.metrics.strike_support_resistance for anyone who needs
that finer granularity.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from .metrics import compute_iv_rank, compute_pcr
from .models import OptionChainSnapshot, OptionContract

# manual section 9: "IV crush warning" / "high-decay no-trade zone".
# Thresholds named as constants for the same reason as everywhere else
# in this codebase (apps/signals/engine.py's TECHNICAL_SCORE_THRESHOLD,
# apps/market_data/regime.py's ADX_TRENDING_THRESHOLD) -- one obvious
# place to tune, and self-documenting at the call site.
IV_RANK_CRUSH_WARNING = 80.0  # IV rank this high often precedes a post-event IV collapse
EXPIRY_PINNING_PROXIMITY_PCT = 0.5  # underlying within 0.5% of max pain, on expiry day itself
HIGH_DECAY_DAYS_TO_EXPIRY = 1  # last trading day before expiry -- theta decay dominates


def _recent_snapshots_by_contract(contracts, n: int) -> dict[int, list[OptionChainSnapshot]]:
    """
    Bulk-fetches the n most recent OptionChainSnapshot rows per contract
    in ONE query via a window function, instead of a separate query per
    contract. A real NIFTY/BANKNIFTY expiry regularly has 200+ live
    contracts, and _aggregate_side_signal/_check_iv_crush below used to
    each run 1-2 queries per contract in a Python loop -- 400+
    individual round trips for a single /api/options/analytics/
    request, which is what made that endpoint take minutes to respond
    instead of the sub-second the same math takes as one query.
    Returns {contract_id: [snapshot, ...]} newest-first per contract.
    """
    contract_ids = [c.pk for c in contracts]
    if not contract_ids:
        return {}
    ranked = OptionChainSnapshot.objects.filter(contract_id__in=contract_ids).annotate(
        _rn=Window(expression=RowNumber(), partition_by=F("contract_id"), order_by=F("timestamp").desc())
    )
    rows = (
        OptionChainSnapshot.objects.filter(pk__in=ranked.filter(_rn__lte=n).values("pk"))
        .order_by("contract_id", "-timestamp")
    )
    by_contract: dict[int, list[OptionChainSnapshot]] = {}
    for row in rows:
        by_contract.setdefault(row.contract_id, []).append(row)
    return by_contract


def _oi_and_price_change(recent: list[OptionChainSnapshot]) -> dict | None:
    """
    Returns {"oi_change_pct", "price_change_pct"} comparing the two
    most recent snapshots (newest-first, as returned by
    _recent_snapshots_by_contract) for one contract, or None if there
    aren't at least two snapshots yet (a freshly-added contract with
    only one reading can't have a "change" computed). Pure computation,
    no DB access -- the caller already bulk-fetched every contract's
    recent snapshots in one query.
    """
    if len(recent) < 2:
        return None

    latest, previous = recent[0], recent[1]
    oi_change_pct = (
        (latest.open_interest - previous.open_interest) / previous.open_interest * 100
        if previous.open_interest else 0.0
    )
    price_change_pct = (
        (float(latest.ltp) - float(previous.ltp)) / float(previous.ltp) * 100
        if previous.ltp else 0.0
    )
    return {"oi_change_pct": oi_change_pct, "price_change_pct": price_change_pct}


def _aggregate_side_signal(underlying: str, expiry: date, option_type: str) -> str | None:
    """
    Aggregates OI-weighted price/OI change across all contracts on one
    side (all CEs, or all PEs) for the expiry, then classifies into one
    of the four buildup/unwinding categories using the OI+price
    combination described in the module docstring. Weighting by OI
    (not a simple average) means strikes with real open interest drive
    the reading, not thinly-traded far-OTM strikes with noisy price
    ticks and negligible OI.
    """
    contracts = list(OptionContract.objects.filter(underlying=underlying, expiry=expiry, option_type=option_type))
    recent_by_contract = _recent_snapshots_by_contract(contracts, 2)
    weighted_oi_change = 0.0
    weighted_price_change = 0.0
    total_weight = 0.0

    for contract in contracts:
        recent = recent_by_contract.get(contract.pk, [])
        change = _oi_and_price_change(recent)
        if change is None:
            continue
        weight = recent[0].open_interest or 1
        weighted_oi_change += change["oi_change_pct"] * weight
        weighted_price_change += change["price_change_pct"] * weight
        total_weight += weight

    if total_weight == 0:
        return None

    avg_oi_change = weighted_oi_change / total_weight
    avg_price_change = weighted_price_change / total_weight

    oi_rising = avg_oi_change > 1.0  # small dead-zone so noise doesn't flip the classification
    price_rising = avg_price_change > 0.5

    if oi_rising and price_rising:
        return "buildup_bullish" if option_type == "CE" else "buildup_bearish"
    if oi_rising and not price_rising:
        return "buildup_bearish" if option_type == "CE" else "buildup_bullish"
    if not oi_rising and price_rising:
        # Same CE/PE flip as the two buildup branches above, and for the
        # same reason: "OI down + premium up" is short covering (bullish)
        # on the CALL side (short call holders buying back as the
        # underlying rallies against them), but on the PUT side that same
        # OI/premium combination is short PUT covering -- writers buying
        # back their puts because the underlying is FALLING against them
        # -- which is bearish, i.e. the call-side's "long_unwinding" case.
        return "short_covering" if option_type == "CE" else "long_unwinding"
    # "OI down + premium down": long call unwinding (bearish) on the CALL
    # side, but long PUT unwinding (bearish-bet holders giving up as the
    # underlying rises) on the PUT side is bullish -- the call-side's
    # "short_covering" case.
    return "long_unwinding" if option_type == "CE" else "short_covering"


def evaluate_options_signals(underlying: str, expiry: date) -> dict:
    """
    Returns a dict of every manual-section-9 options signal that's
    currently applicable, each as {"flag": bool, "detail": str} so a
    caller (apps.signals.engine, or the frontend's Options Analytics
    page) gets both the boolean AND the reasoning in one call --
    matches the "AI must explain every signal" principle from the
    manual's Core Principles, applied at the options-flow layer too,
    not just the final trade signal.
    """
    result = {}

    call_side = _aggregate_side_signal(underlying, expiry, "CE")
    put_side = _aggregate_side_signal(underlying, expiry, "PE")

    result["bullish_call_buildup"] = {
        "flag": call_side == "buildup_bullish",
        "detail": "Rising call OI with rising call premiums -- fresh bullish call buying." if call_side == "buildup_bullish" else "",
    }
    result["bearish_put_buildup"] = {
        "flag": put_side == "buildup_bearish",
        "detail": "Rising put OI with rising put premiums -- fresh bearish put buying." if put_side == "buildup_bearish" else "",
    }
    result["short_covering"] = {
        "flag": call_side == "short_covering" or put_side == "short_covering",
        "detail": "Falling OI with rising premiums -- short positions being closed out." if (
            call_side == "short_covering" or put_side == "short_covering"
        ) else "",
    }
    result["long_unwinding"] = {
        "flag": call_side == "long_unwinding" or put_side == "long_unwinding",
        "detail": "Falling OI with falling premiums -- long positions being closed out." if (
            call_side == "long_unwinding" or put_side == "long_unwinding"
        ) else "",
    }

    result["expiry_pinning_risk"] = _check_expiry_pinning(underlying, expiry)
    result["iv_crush_warning"] = _check_iv_crush(underlying, expiry)
    result["high_decay_no_trade_zone"] = _check_high_decay_zone(expiry)

    return result


def nearest_expiry(underlying: str) -> date | None:
    """
    Earliest CURRENT-OR-FUTURE expiry synced for this underlying --
    used by apps.signals.engine so the composite signal engine doesn't
    need its own copy of "which expiry does the options confirmation
    apply to" logic. Returns None if apps.options.tasks hasn't synced
    any contracts yet (fresh install / options sync hasn't run), OR if
    every synced expiry has already passed (sync_watchlist_option_
    contracts hasn't run since the last one expired -- e.g. BROKER_MODE
    isn't "live", or Celery beat isn't running) -- either way, callers
    treat "no usable options data right now" the same "not an error,
    just not there yet" way compute_indicators() does.

    The expiry__gte filter matters: without it, a stale DB whose only
    synced expiry already passed would silently return that dead
    expiry as "nearest," and every caller (options_confluence_score
    below, apps.options.strike_selector, apps.jarvis) would then score/
    suggest strikes against contracts that can no longer actually be
    traded -- a real bug this exact shape surfaced in practice (a
    single 2026-08-11 expiry synced once, still being read as "nearest"
    a day after it expired).
    """
    return (
        OptionContract.objects.filter(underlying=underlying, expiry__gte=timezone.localdate())
        .order_by("expiry").values_list("expiry", flat=True).first()
    )


def select_expiry(underlying: str, mode: str | None = None) -> date | None:
    """
    The configurable counterpart to nearest_expiry() above (which stays
    untouched -- other callers, e.g. apps.jarvis, keep using it exactly
    as before). mode: "current_week"/"next_week"/"monthly"/"custom"
    (case-insensitive), or None to read apps.options.models.
    OptionsStrategySetting.expiry_mode.

    Every mode still only ever returns an expiry that ALREADY has synced
    OptionContract rows for this underlying -- "what the correct expiry
    is" (a live-data question, answered from the real broker instrument
    master for MONTHLY) is kept separate from "have we synced it yet" (a
    local-data question), so a not-yet-synced correct expiry returns
    None (an honest "not there yet") rather than silently falling back
    to nearest_expiry's answer and trading the wrong expiry.
    """
    from .models import get_options_strategy_settings

    if mode is None:
        mode = get_options_strategy_settings().expiry_mode
    mode = mode.lower()

    synced = list(
        OptionContract.objects.filter(underlying=underlying, expiry__gte=timezone.localdate())
        .order_by("expiry").values_list("expiry", flat=True).distinct()
    )
    if not synced:
        return None

    if mode == "current_week":
        return synced[0]

    if mode == "next_week":
        return synced[1] if len(synced) > 1 else None

    if mode == "monthly":
        # "Monthly" isn't a flag Angel One's instrument master exposes
        # directly -- it's the LAST weekly expiry that falls within the
        # earliest calendar month among the underlying's real listed
        # expiries (NSE's actual monthly-expiry convention). Determined
        # from live broker data (list_expiries), not merely from
        # whatever happens to already be synced, so the answer doesn't
        # depend on how many expiries a past sync run happened to pull.
        from .instrument_master import list_expiries

        listed = list_expiries(underlying, limit=12)
        if not listed:
            return None
        earliest_month = (listed[0].year, listed[0].month)
        in_month = [e for e in listed if (e.year, e.month) == earliest_month]
        monthly_expiry = max(in_month)
        return monthly_expiry if monthly_expiry in synced else None

    if mode == "custom":
        from .models import get_options_strategy_settings

        custom = get_options_strategy_settings().custom_expiry
        if custom is None or custom not in synced:
            return None
        return custom

    return None


# How many of the *directional* options-flow signals (buildup/covering/
# unwinding -- excludes the risk-only flags below) must agree with the
# technical setup's direction for options to count as confirming it.
# Deliberately not "all of them": short_covering and long_unwinding can
# legitimately be absent even in a genuine bullish/bearish setup (they
# only fire when positions are actively being closed out), so requiring
# every flag would make options confirmation almost never trigger.
_OPTIONS_CONFIRMATION_WEIGHT = {
    "bullish_call_buildup": "bullish",
    "bearish_put_buildup": "bearish",
    "short_covering": "bullish",
    "long_unwinding": "bearish",
}


def options_confluence_score(underlying: str, direction: str = "bullish") -> dict:
    """
    Folds apps.options.signals_engine's per-contract OI/price flow
    reading, PCR, and the manual's risk flags (IV crush, expiry
    pinning, high-decay zone) into one 0..1 confirmation score plus a
    hard veto -- this is what apps.signals.engine.generate_signal()
    calls so option-chain positioning becomes a real confirming/vetoing
    factor for equity/index signals, not just a page nobody's technical
    signal actually consults.

    `direction` is "bullish" or "bearish" (which side the technical
    setup is already leaning, from apps.signals.engine's own EMA/MACD
    conditions) -- options confirmation is direction-aware for the same
    reason sentiment is: agreeing with an already-bearish setup is not
    the same evidence as agreeing with a bullish one.

    Returns {"score": float 0..1, "veto": bool, "veto_reason": str,
    "detail": str} -- score contributes to total_score like
    technical/sentiment/risk already do; veto=True should reject the
    trade outright regardless of score (same pattern as sentiment's
    contradictory-headline veto in apps.signals.engine).
    """
    expiry = nearest_expiry(underlying)
    if expiry is None:
        # No options data synced for this underlying -- neutral, not a
        # veto: the composite engine can still trade on technicals
        # alone rather than being blocked by an infra gap.
        return {"score": 0.5, "veto": False, "veto_reason": "", "detail": "No options data synced yet."}

    signals = evaluate_options_signals(underlying, expiry)

    if signals["iv_crush_warning"]["flag"]:
        return {
            "score": 0.0, "veto": True,
            "veto_reason": signals["iv_crush_warning"]["detail"],
            "detail": signals["iv_crush_warning"]["detail"],
        }
    if signals["high_decay_no_trade_zone"]["flag"]:
        return {
            "score": 0.0, "veto": True,
            "veto_reason": signals["high_decay_no_trade_zone"]["detail"],
            "detail": signals["high_decay_no_trade_zone"]["detail"],
        }
    if signals["expiry_pinning_risk"]["flag"]:
        return {
            "score": 0.0, "veto": True,
            "veto_reason": signals["expiry_pinning_risk"]["detail"],
            "detail": signals["expiry_pinning_risk"]["detail"],
        }

    agreeing = [
        name for name, side in _OPTIONS_CONFIRMATION_WEIGHT.items()
        if signals[name]["flag"] and side == direction
    ]
    disagreeing = [
        name for name, side in _OPTIONS_CONFIRMATION_WEIGHT.items()
        if signals[name]["flag"] and side != direction
    ]

    pcr = compute_pcr(underlying, expiry)
    pcr_agrees = pcr is not None and (
        (direction == "bullish" and pcr > 1.0) or (direction == "bearish" and pcr < 1.0)
    )

    # Base 0.5 (neutral -- no options flow one way or the other is not
    # itself bearish on the setup), +0.25 per agreeing flow signal,
    # -0.25 per disagreeing one, +0.15 if PCR agrees, clamped to 0..1.
    score = 0.5 + 0.25 * len(agreeing) - 0.25 * len(disagreeing) + (0.15 if pcr_agrees else 0.0)
    score = max(0.0, min(1.0, score))

    detail_parts = []
    if agreeing:
        detail_parts.append(f"Options flow agrees ({', '.join(agreeing)}).")
    if disagreeing:
        detail_parts.append(f"Options flow disagrees ({', '.join(disagreeing)}).")
    if pcr is not None:
        detail_parts.append(f"PCR={pcr:.2f}{' (agrees)' if pcr_agrees else ''}.")
    if not detail_parts:
        detail_parts.append("No directional options flow detected -- neutral.")

    return {"score": round(score, 4), "veto": False, "veto_reason": "", "detail": " ".join(detail_parts)}


def _check_expiry_pinning(underlying: str, expiry: date) -> dict:
    from .metrics import compute_max_pain

    today = timezone.localdate()
    if today != expiry:
        return {"flag": False, "detail": ""}

    max_pain = compute_max_pain(underlying, expiry)
    if max_pain is None:
        return {"flag": False, "detail": "Not enough data to compute max pain."}

    latest_underlying_price = _latest_underlying_ltp(underlying, expiry)
    if latest_underlying_price is None:
        return {"flag": False, "detail": ""}

    distance_pct = abs(latest_underlying_price - max_pain) / max_pain * 100
    if distance_pct <= EXPIRY_PINNING_PROXIMITY_PCT:
        return {
            "flag": True,
            "detail": (
                f"Underlying ({latest_underlying_price}) is within "
                f"{distance_pct:.2f}% of max pain ({max_pain}) on expiry day -- "
                f"elevated pinning risk."
            ),
        }
    return {"flag": False, "detail": ""}


def _latest_underlying_ltp(underlying: str, expiry: date) -> float | None:
    """
    The underlying's actual most recent close from
    apps.market_data.HistoricalData -- the same real candle data
    apps.market_data.tasks ingests for every WATCHLIST symbol. `expiry`
    is unused (kept in the signature for backward compatibility with
    existing callers) -- the underlying's price doesn't depend on which
    options expiry is being evaluated.

    Returns None if no candle has been ingested for this underlying yet
    (e.g. it isn't in settings.WATCHLIST, or ingestion hasn't run) --
    callers already treat None as "can't compute this check yet," not
    an error, matching every other "missing data isn't a hard block"
    stance in this module.
    """
    from apps.market_data.models import HistoricalData

    latest = HistoricalData.objects.filter(symbol=underlying).order_by("-timestamp").first()
    return float(latest.close) if latest else None


def _check_iv_crush(underlying: str, expiry: date) -> dict:
    contracts = list(OptionContract.objects.filter(underlying=underlying, expiry=expiry))
    recent_by_contract = _recent_snapshots_by_contract(contracts, 20)
    high_iv_rank_contracts = []

    for contract in contracts:
        recent_snapshots = recent_by_contract.get(contract.pk, [])
        if len(recent_snapshots) < 2:
            continue
        current_iv = recent_snapshots[0].iv
        historical_ivs = [s.iv for s in recent_snapshots[1:] if s.iv is not None]
        if current_iv is None or not historical_ivs:
            continue
        rank = compute_iv_rank(current_iv, historical_ivs)
        if rank is not None and rank >= IV_RANK_CRUSH_WARNING:
            high_iv_rank_contracts.append((contract, rank))

    if high_iv_rank_contracts:
        worst = max(high_iv_rank_contracts, key=lambda pair: pair[1])
        return {
            "flag": True,
            "detail": (
                f"{worst[0]} has IV rank {worst[1]:.0f} (>= {IV_RANK_CRUSH_WARNING:.0f}) -- "
                f"elevated risk of an IV crush after the next major event/expiry."
            ),
        }
    return {"flag": False, "detail": ""}


def _check_high_decay_zone(expiry: date) -> dict:
    days_to_expiry = (expiry - timezone.localdate()).days
    if 0 <= days_to_expiry <= HIGH_DECAY_DAYS_TO_EXPIRY:
        return {
            "flag": True,
            "detail": (
                f"{days_to_expiry} day(s) to expiry -- theta decay dominates; "
                f"buying premium here needs a strong, immediate move to be worth the decay risk."
            ),
        }
    return {"flag": False, "detail": ""}
