"""
TradeOutcomeService -- MFE/MAE/R-multiple/outcome-classification for
CLOSED trades, and the hypothetical-outcome walk-forward for HOLD/SKIP
decisions that never opened a real PaperOrder (spec: "Do not train only
on completed trades... calculate the later hypothetical outcome without
creating a paper order").

Outcome classifications are statistical diagnostic labels from simple,
documented heuristics -- NOT a claim of causal truth (spec's own
caveat, repeated here deliberately).
"""

from __future__ import annotations

from decimal import Decimal

from ..models import OutcomeClassification, PaperTradeResult


def _classify(*, direction_correct: bool, exit_early: bool, exit_late: bool, stop_too_tight: bool) -> str:
    if not direction_correct:
        return OutcomeClassification.WRONG_DIRECTION
    if stop_too_tight:
        return OutcomeClassification.CORRECT_DIRECTION_STOP_TOO_TIGHT
    if exit_early:
        return OutcomeClassification.EARLY_EXIT
    if exit_late:
        return OutcomeClassification.LATE_EXIT
    return OutcomeClassification.CORRECT_DIRECTION_GOOD_ENTRY


def finalize_trade_outcome(trade, position, exit_reason: str) -> PaperTradeResult:
    entry = position.average_entry_price
    mfe = max(Decimal(0), (position.peak_favorable_price or entry) - entry)
    mae = max(Decimal(0), entry - (position.trough_adverse_price or entry))
    initial_risk = entry - position.initial_stop_loss
    r_multiple = (
        (trade.net_pnl / (initial_risk * position.quantity))
        if initial_risk and initial_risk > 0 else None
    )
    direction_correct = trade.net_pnl > 0
    time_in_trade = int((trade.closed_at - position.opened_at).total_seconds())

    favorable_at_exit = trade.exit_price - entry
    # Heuristics, deliberately simple and documented (module docstring):
    # "early" = exited with less than half the trade's own peak
    # favorable excursion captured, while still net favorable. "late" =
    # gave back a large share of that peak before exiting. "stop too
    # tight" = direction ended up wrong, but favorable excursion was at
    # least double the adverse excursion actually reached before the
    # stop presumably triggered -- suggests price would likely have come
    # back had the stop been wider.
    exit_early = bool(mfe > 0 and Decimal(0) < favorable_at_exit < mfe * Decimal("0.5"))
    exit_late = bool(mfe > 0 and mae > mfe * Decimal("0.3") and favorable_at_exit > 0)
    stop_too_tight = bool(not direction_correct and mfe > mae * 2)

    outcome = _classify(
        direction_correct=direction_correct, exit_early=exit_early,
        exit_late=exit_late, stop_too_tight=stop_too_tight,
    )
    if not direction_correct and "decay" in exit_reason.lower():
        outcome = OutcomeClassification.OPTION_DECAY_LOSS

    return PaperTradeResult.objects.create(
        trade=trade,
        initial_stop_distance=initial_risk if initial_risk and initial_risk > 0 else Decimal("0.01"),
        stop_modifications_json=[], mfe=mfe, mae=mae, r_multiple=r_multiple,
        time_in_trade_seconds=max(0, time_in_trade),
        best_unrealized_profit=(mfe * position.quantity), worst_unrealized_loss=(mae * position.quantity),
        exit_reason=exit_reason, direction_correct=direction_correct,
        stop_too_tight=stop_too_tight, exit_early=exit_early, exit_late=exit_late,
        outcome_classification=outcome,
    )


def compute_hypothetical_outcome(symbol: str, timeframe: str, decision_timestamp, entry_reference: float, horizon_bars: int = 12) -> dict:
    """Walks completed candles forward from `decision_timestamp` on the
    UNDERLYING (not a specific, never-selected option contract -- there
    is none for a HOLD/SKIP) to see what a directional trade would have
    captured. `entry_reference` is normally the underlying close at
    decision time (feature_snapshot_json["close"])."""
    from apps.market_data.models import HistoricalData

    forward = list(
        HistoricalData.objects.filter(symbol=symbol, timeframe=timeframe, timestamp__gt=decision_timestamp)
        .order_by("timestamp")[:horizon_bars]
    )
    if not forward or entry_reference <= 0:
        return {"available": False}

    highs = [float(c.high) for c in forward]
    lows = [float(c.low) for c in forward]
    last_close = float(forward[-1].close)
    return {
        "available": True,
        "bars_forward": len(forward),
        "mfe": max(0.0, max(highs) - entry_reference),
        "mae": max(0.0, entry_reference - min(lows)),
        "move_pct": (last_close - entry_reference) / entry_reference * 100,
        "final_close": last_close,
    }
