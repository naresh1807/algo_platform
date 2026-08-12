"""
The one function every other app should call to record a governance-
relevant action. Kept deliberately tiny and defensive (never raises)
-- an audit log write failing must never be allowed to block or crash
the actual action being logged (e.g. a kill-switch re-arm should still
succeed even if, somehow, the audit table write itself has a problem;
better to have the real action succeed with a missing log line than to
have the log succeed but the safety action fail).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def log_action(
    action: str,
    actor=None,
    actor_label: str = "",
    target=None,
    details: dict | None = None,
) -> None:
    """
    actor: a Django User instance, or None for system/scheduled actions
        (pass actor_label instead, e.g. "celery:check_for_drift").
    target: any model instance -- its class name and pk are recorded
        (target_type/target_id) without needing a real ForeignKey to
        every possible model this could ever point at (a generic
        audit log pointing at StrategyVersion, OpenPosition, RiskEvent,
        DailyReviewNote, etc. via real FKs would mean either one FK
        column per possible target type, or a GenericForeignKey with
        its own complexity -- plain string type+id is simpler and is
        all a human reading the log actually needs).
    details: any extra JSON-serializable context worth recording.
    """
    from .models import AuditLog

    try:
        AuditLog.objects.create(
            actor=actor,
            actor_label=actor_label,
            action=action,
            target_type=type(target).__name__ if target is not None else "",
            target_id=str(getattr(target, "pk", "")) if target is not None else "",
            details=details or {},
        )
    except Exception:
        # Deliberately swallowed (with a loud log line) -- see module
        # docstring for why an audit-write failure must never propagate
        # up and block the real action.
        logger.exception("Failed to write audit log entry for action=%s", action)
