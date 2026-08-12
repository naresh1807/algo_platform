"""
manual 11.13 "AI Mistake Analysis" / 13.14 "Mistake Analysis":
classifies a CLOSED trade into zero or more of the manual's named
categories, using only data this scaffold actually has (OpenPosition +
its TradingSignal + sibling same-day positions) -- no category is
inferred from data that doesn't exist. A trade can match more than one
category, or none.

Honest gap: the manual lists seven categories. Six are implemented
below with a real, inspectable rule each. "Late Entry" is NOT
implemented -- apps.execution.paper_executor/live_executor fill at the
signal's own entry_price (there is no separate "price at the moment
the order was actually placed" field to compare it against), so there
is no real data to detect entry lag from. Faking it off holding-time
or candle count would be guessing, not measuring -- so it's left out
rather than filled in with something that looks like a real signal but
isn't. If a real fill-price/fill-time field is added later (as
live_executor's fill-confirmation polling already tracks internally),
this is the place to add it.
"""

from __future__ import annotations

from .ml_predict import MIN_WIN_PROBABILITY_TO_TRADE
from .reward import compute_r_multiple

FALSE_BREAKOUT_MINUTES = 10
EARLY_EXIT_MINUTES = 15
HIGH_RISK_R_THRESHOLD = -1.2  # loss worse than the planned stop distance -- a gap through the stop
OVERTRADING_SAME_SYMBOL_SAME_DAY = 3  # this position is at least the (N+1)th on the same symbol today


def classify_mistakes(position) -> list[str]:
    if not position.closed_at:
        return []

    signal = position.signal
    holding_seconds = (position.closed_at - position.opened_at).total_seconds()
    r = compute_r_multiple(position)

    tags: list[str] = []

    # False Breakout: stopped out (or close to full planned loss) within
    # minutes of entry -- the level reversed almost immediately rather
    # than the setup ever having room to work.
    if r is not None and r <= -0.9 and holding_seconds < FALSE_BREAKOUT_MINUTES * 60:
        tags.append("False Breakout")

    # Early Exit: closed quickly, roughly flat-to-small-loss, well
    # short of the target -- consistent with bailing out before the
    # setup had time to play out, rather than a clean stop or target hit.
    if (
        position.target_price is not None
        and holding_seconds < EARLY_EXIT_MINUTES * 60
        and r is not None and -0.9 < r < 0.5
    ):
        planned_move = float(position.qty) * float(abs(position.target_price - position.entry_price))
        if planned_move > 0 and abs(float(position.unrealized_pnl)) < planned_move * 0.5:
            tags.append("Early Exit")

    # Low Confidence: taken despite the ML model itself scoring it under
    # the system's own reject bar at signal time -- can only happen for
    # trades approved before the model existed/changed, or edge timing
    # around a model promotion; flagging it is still useful even then.
    if signal and signal.ml_win_probability is not None and signal.ml_win_probability < MIN_WIN_PROBABILITY_TO_TRADE:
        tags.append("Low Confidence")

    # Wrong Trend: entered while the regime at signal time was NOT
    # "trending" (i.e. sideways or high_volatility -- see
    # common.constants.MarketRegime), and the trade lost.
    if signal and signal.regime and signal.regime != "trending" and r is not None and r < 0:
        tags.append("Wrong Trend")

    # High Risk: the realized loss was worse than the planned stop
    # distance itself -- a gap through the stop, not a clean stop-out.
    if r is not None and r < HIGH_RISK_R_THRESHOLD:
        tags.append("High Risk")

    # Over Trading: at least the (N+1)th position opened on this same
    # symbol on the same calendar day. Deliberately single-symbol only
    # -- a real cross-symbol/portfolio-wide overtrading check would need
    # a dedicated daily-trade-count view this scaffold doesn't have yet.
    from apps.execution.models import OpenPosition

    same_day_same_symbol = OpenPosition.objects.filter(
        symbol=position.symbol, opened_at__date=position.opened_at.date(),
    ).count()
    if same_day_same_symbol > OVERTRADING_SAME_SYMBOL_SAME_DAY:
        tags.append("Over Trading")

    return tags
