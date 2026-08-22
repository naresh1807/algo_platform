"""
PaperStateMachine -- explicit FSM enforcement for the autonomous loop.
Transition HISTORY is written via apps.admin_tools.audit.log_action
(the same pattern apps.execution.paper_executor already uses for its
own action logging), not a dedicated table -- see models.py's
PaperSessionState docstring for why. ERROR_SAFE_MODE/TRADING_STOPPED/
FLAT are always reachable as safety escapes from any state, matching
this codebase's general "the safe/halted state must never be
unreachable" posture (compare apps.risk.engine.trigger_kill_switch).
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from ..models import PaperSessionState

_ALWAYS_REACHABLE = {
    PaperSessionState.State.ERROR_SAFE_MODE,
    PaperSessionState.State.TRADING_STOPPED,
    PaperSessionState.State.FLAT,
}

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    PaperSessionState.State.FLAT: {"analyzing", "market_closed", "trading_stopped", "risk_blocked", "feed_stale"},
    PaperSessionState.State.ANALYZING: {"entry_candidate", "flat", "market_closed", "risk_blocked", "feed_stale", "cooldown", "trading_stopped"},
    PaperSessionState.State.ENTRY_CANDIDATE: {"order_pending", "flat", "risk_blocked"},
    PaperSessionState.State.ORDER_PENDING: {"position_open", "flat"},
    PaperSessionState.State.POSITION_OPEN: {"position_management", "exit_pending"},
    PaperSessionState.State.POSITION_MANAGEMENT: {"exit_pending", "position_open"},
    PaperSessionState.State.EXIT_PENDING: {"position_closed"},
    PaperSessionState.State.POSITION_CLOSED: {"cooldown", "flat", "analyzing"},
    PaperSessionState.State.COOLDOWN: {"flat", "analyzing"},
    PaperSessionState.State.FEED_STALE: {"flat", "analyzing", "trading_stopped"},
    PaperSessionState.State.RISK_BLOCKED: {"flat", "cooldown", "trading_stopped"},
    PaperSessionState.State.TRADING_STOPPED: set(),
    PaperSessionState.State.MARKET_CLOSED: {"flat"},
    PaperSessionState.State.ERROR_SAFE_MODE: set(),
}


def current_state() -> PaperSessionState:
    row, _ = PaperSessionState.objects.get_or_create(pk=1)
    return row


def transition(to_state: str, *, reason: str = "", context: dict | None = None) -> PaperSessionState:
    from apps.admin_tools.audit import log_action

    row = current_state()
    from_state = row.state
    if to_state != from_state and to_state not in _ALWAYS_REACHABLE:
        allowed = _ALLOWED_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise ValueError(f"Illegal paper-trading state transition: {from_state} -> {to_state}")

    row.state = to_state
    row.context_json = context or {}
    if to_state == PaperSessionState.State.COOLDOWN:
        from django.conf import settings

        row.cooldown_until = timezone.now() + timedelta(minutes=settings.AI_PAPER_COOLDOWN_MINUTES)
    else:
        row.cooldown_until = None
    row.save()
    log_action(
        "paper_trading_state_transition", actor_label="apps.paper_trading",
        details={"from": from_state, "to": to_state, "reason": reason},
    )
    return row


def cooldown_expired() -> bool:
    row = current_state()
    if row.state != PaperSessionState.State.COOLDOWN:
        return False
    return row.cooldown_until is not None and timezone.now() >= row.cooldown_until
