"""
Turns a closed apps.execution.models.OpenPosition into a
TradeReview -- manual 13.8 "Experience Memory". Registered in
apps/learning/apps.py's ready(), same pattern as apps.jarvis.signals
turning other apps' events into JARVIS announcements.

Uses the same update_fields-based "did this save actually close the
position" detection as apps.jarvis.signals._announce_position_closed,
for the same reason documented there (OpenPosition has no dedicated
closed-event field) -- and the same fragility note applies: if
paper_executor/live_executor's close_position() ever stops passing an
explicit update_fields list including "closed_at", this stops firing
silently rather than erroring loudly. A dedicated PositionClosed event
would be the more robust long-term fix.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.execution.models import OpenPosition

from .confidence import confidence_band
from .mistakes import classify_mistakes
from .reward import compute_reward_score

logger = logging.getLogger(__name__)


@receiver(post_save, sender=OpenPosition)
def _create_trade_review(sender, instance: OpenPosition, created, update_fields, **kwargs):
    if created or not instance.closed_at:
        return
    if update_fields is not None and "closed_at" not in update_fields:
        return

    from .models import TradeReview

    if TradeReview.objects.filter(position=instance).exists():
        return  # already reviewed -- close_position() should only fire this once per position

    try:
        r_multiple, reward_score = compute_reward_score(instance)
        signal = instance.signal
        band = confidence_band(signal.ml_win_probability if signal else None)
        tags = classify_mistakes(instance)

        TradeReview.objects.create(
            position=instance,
            r_multiple=r_multiple,
            reward_score=reward_score,
            confidence_band=band,
            mistake_tags=tags,
        )
    except Exception:
        # Same defensive posture as apps.jarvis.notify.announce /
        # apps.admin_tools.audit.log_action -- a review-generation
        # failure must never block or roll back the real trade-closing
        # transaction that triggered it.
        logger.exception("Failed to create TradeReview for OpenPosition %s", instance.pk)
