"""
manual 11.10 "Reward Engine" / 13.9 "Reward System": every closed
paper trade gets a discrete reward score -- the same kind of signal a
real reinforcement-learning update rule would consume, kept here as a
plain, transparent function rather than baked into any training code,
matching this codebase's general "explainable, inspectable heuristics
over an opaque score" stance (see apps/risk/engine.py's own docstring
on the same principle).

R-multiple (P&L divided by the trade's own planned risk -- exactly the
qty x |entry - stop| amount apps.risk.engine already sizes every trade
against) is the basis for the bands, not a raw currency figure, so the
score means the same thing regardless of position size or account
size.

Honest assumption: the manual gives point EXAMPLES (+10 profit, +20
large profit, -5 small loss, -20 large loss), not exact thresholds or
an explicit "neutral" score. This is the most literal reading of that
example table, with "large" read as roughly double a normal
profit/loss and "neutral" (manual 13.9: "small reward") set to a small
positive score -- documented here rather than silently guessed:

    R >= +2.0   -> +20  (Large Profit)
    R >  0.0    -> +10  (Profit)
    R == 0.0    ->  +2  (Neutral)
    R > -1.0    ->  -5  (Small Loss)
    R <= -1.0   -> -20  (Large Loss -- lost at least the full planned risk)
"""

from __future__ import annotations


def compute_r_multiple(position) -> float | None:
    """
    position: a closed apps.execution.models.OpenPosition.
    Returns None if there's no real risk distance to divide by (stop_loss
    equal to entry_price) -- shouldn't happen for anything
    apps.risk.engine approved, but stays defensive rather than raising
    on bad/legacy data.
    """
    risk_amount = float(position.qty) * float(abs(position.entry_price - position.stop_loss))
    if risk_amount <= 0:
        return None
    return float(position.unrealized_pnl) / risk_amount


def compute_reward_score(position) -> tuple[float | None, int | None]:
    """Returns (r_multiple, reward_score) -- both None if unscoreable."""
    r = compute_r_multiple(position)
    if r is None:
        return None, None
    if r >= 2.0:
        return r, 20
    if r > 0.0:
        return r, 10
    if r == 0.0:
        return r, 2
    if r > -1.0:
        return r, -5
    return r, -20
