"""
AIExitManager -- stop/target trigger detection and the AI's bounded
stop-management actions (KEEP_CURRENT_STOP/TIGHTEN_STOP/TRAIL_STOP).
The hard "never widen past the current level" boundary lives in
position_service.tighten_stop/apply_trailing_stop, not here -- this
module only decides WHICH of those to call.
"""

from __future__ import annotations

from decimal import Decimal

from . import position_service


def evaluate_hard_exit(position, quote) -> tuple[str | None, str]:
    """Stop-loss/target trigger check against a conservative executable
    mark (bid, not LTP -- spec: "Use a conservative executable mark price
    for risk and exit calculations"). Priority: stop-loss first, then
    target -- matches apps.execution.paper_executor's own exit-priority
    convention."""
    mark_source = quote.bid if quote.bid is not None else quote.ltp
    if mark_source is None:
        return None, ""
    mark = Decimal(str(mark_source))
    if mark <= position.stop_loss:
        return "stop_loss", f"mark {mark} <= stop {position.stop_loss}"
    if position.target_price is not None and mark >= position.target_price:
        return "target", f"mark {mark} >= target {position.target_price}"
    return None, ""


def apply_stop_action(position, action: str, *, mark_price: Decimal, atr: Decimal | None = None) -> None:
    """action: one of Action.KEEP_CURRENT_STOP / TIGHTEN_STOP / TRAIL_STOP."""
    if action == "trail_stop":
        position_service.apply_trailing_stop(position, mark_price)
    elif action == "tighten_stop" and atr is not None and atr > 0:
        candidate = mark_price - atr
        if candidate > position.stop_loss:
            position_service.tighten_stop(position, candidate)
    # keep_current_stop -- deliberate no-op
