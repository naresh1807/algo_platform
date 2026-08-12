"""
manual section 16, "Security and Governance Manual": "Audit every
action. Version every strategy. Version every model. Log every signal.
Log every order." Strategy/model versioning and signal logging already
existed (apps.learning.StrategyVersion/ModelRegistry,
apps.signals.TradingSignal) before this app -- what was missing was a
single, generic audit trail for the *administrative/governance* actions
those other logs don't cover: who approved a daily review, who promoted
a strategy version, who re-armed the kill switch, who (or what process)
placed/rejected an order.

Deliberately ONE generic table (not a separate audit model per app) --
manual section 16's own principle ("Trading platforms must be auditable
before they are clever") is best served by one place a human can look
to answer "who did what, when", rather than needing to know which of
nine different per-app tables might have the answer.
"""

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """
    actor is nullable + SET_NULL (not CASCADE, not PROTECT) on purpose:
    an audit log entry must NEVER be deleted just because the user
    account that performed the action later gets deleted -- the record
    of "someone did X at time T" needs to survive even if we lose the
    ability to say exactly who. actor=None also covers actions taken by
    a scheduled/system process (e.g. the risk engine auto-triggering
    the kill switch) rather than a human -- see `actor_label` for how
    those are still attributed to something readable.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="audit_log_entries",
    )
    actor_label = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable actor for system-triggered actions with no "
                  "Django user attached, e.g. 'risk_engine' or 'celery:run_daily_review'.",
    )
    action = models.CharField(
        max_length=100, db_index=True,
        help_text="e.g. kill_switch_rearmed, strategy_version_promoted, "
                  "daily_review_approved, order_placed, order_rejected",
    )
    target_type = models.CharField(max_length=100, blank=True, help_text="e.g. StrategyVersion, OpenPosition")
    target_id = models.CharField(max_length=100, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["action", "-created_at"])]

    def __str__(self):
        who = self.actor.username if self.actor else (self.actor_label or "unknown")
        return f"{who}: {self.action} ({self.target_type} {self.target_id}) @ {self.created_at}"
