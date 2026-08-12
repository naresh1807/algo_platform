"""
Turns real events from OTHER apps into JARVIS announcements (manual
14.16: "JARVIS Announces" -- Trade Executed, Signal Generated, Risk
Warning, Kill Switch, AI Training Completed). Registered in
apps/jarvis/apps.py's ready().

Honest limitation: OpenPosition has no dedicated "just closed" event --
apps.execution.paper_executor.close_position() sets closed_at and
saves. post_save can't distinguish "just closed this save" from "saved
again after being closed" without an extra field, so
_announce_position_closed uses update_fields as the signal (paper_
executor's close_position always calls save() with an explicit
update_fields list that includes closed_at) -- if that ever changes,
this announcement could double-fire or miss. A dedicated PositionClosed
event/outbox is the more robust long-term fix; noted here rather than
silently assumed correct.
"""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.execution.models import OpenPosition
from apps.learning.models import DriftEvent, ModelRegistry
from apps.risk.models import KillSwitchState, RiskEvent
from apps.signals.models import TradingSignal

from .notify import announce


@receiver(post_save, sender=TradingSignal)
def _announce_signal(sender, instance: TradingSignal, created, **kwargs):
    if not created:
        return
    announce(
        "signal_generated",
        f"{instance.symbol} {instance.signal_type} ({instance.status}), score {instance.total_score:.2f}",
        symbol=instance.symbol, status=instance.status,
    )


@receiver(post_save, sender=RiskEvent)
def _announce_risk_event(sender, instance: RiskEvent, created, **kwargs):
    if not created or instance.severity not in ("warning", "critical"):
        return
    announce(
        "risk_warning" if instance.severity == "warning" else "risk_critical",
        f"{instance.event_type}: {instance.message[:200]}",
        severity=instance.severity,
    )


@receiver(post_save, sender=KillSwitchState)
def _announce_kill_switch(sender, instance: KillSwitchState, created, **kwargs):
    announce(
        "kill_switch",
        "Kill switch ACTIVATED -- no new trades will be approved." if instance.is_active
        else "Kill switch deactivated.",
        is_active=instance.is_active,
    )


@receiver(post_save, sender=OpenPosition)
def _announce_position_opened(sender, instance: OpenPosition, created, **kwargs):
    """
    manual 14.16 "Trade Executed" -- an order actually got filled and a
    position opened. Fires from a single post_save hook on OpenPosition,
    so it covers apps.execution.paper_executor AND live_executor
    identically (both create the row the same way; see each module's
    docstring on why they share this exact structure) without either
    executor needing to import apps.jarvis directly.
    """
    if not created:
        return
    announce(
        "trade_executed",
        f"{instance.symbol} {instance.side} x{instance.qty} @ {instance.entry_price} -- position opened.",
        symbol=instance.symbol,
    )


@receiver(post_save, sender=OpenPosition)
def _announce_position_closed(sender, instance: OpenPosition, created, update_fields, **kwargs):
    if created or not instance.closed_at:
        return
    if update_fields is not None and "closed_at" not in update_fields:
        return
    announce(
        "position_closed",
        f"{instance.symbol} position closed, P&L {instance.unrealized_pnl}",
        symbol=instance.symbol,
    )


@receiver(post_save, sender=ModelRegistry)
def _announce_model_trained(sender, instance: ModelRegistry, created, **kwargs):
    if not created:
        return
    announce(
        "ai_training_completed",
        f"{instance.model_name} v{instance.model_version} trained"
        + (" and promoted to active." if instance.active_flag else " (not promoted -- challenger)."),
        active=instance.active_flag,
    )


@receiver(post_save, sender=DriftEvent)
def _announce_drift(sender, instance: DriftEvent, created, **kwargs):
    if not created:
        return
    announce(
        "drift_detected",
        f"{instance.drift_type}/{instance.metric_name} drift detected ({instance.severity}).",
        severity=instance.severity,
    )
