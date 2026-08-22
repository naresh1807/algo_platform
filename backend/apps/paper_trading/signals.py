"""
Broadcasts PaperAccount/PaperPosition/PaperAIDecision changes to the
"paper_trading_live" WebSocket group -- same post_save-signal pattern
apps.risk.signals/apps.options.signals already use, via the SAME
common.websockets.broadcast_group (the one persistent-event-loop
plumbing every broadcast in this codebase routes through).
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from common.websockets import broadcast_group

from .models import PaperAccount, PaperAIDecision, PaperPosition
from .serializers import PaperAccountSerializer, PaperAIDecisionSerializer, PaperPositionSerializer

logger = logging.getLogger(__name__)


@receiver(post_save, sender=PaperAccount)
def broadcast_account(sender, instance, **kwargs):
    broadcast_group(
        "paper_trading_live",
        {"type": "paper_trading_update", "data": {"kind": "account", **PaperAccountSerializer(instance).data}},
        log=logger,
    )


@receiver(post_save, sender=PaperPosition)
def broadcast_position(sender, instance, **kwargs):
    broadcast_group(
        "paper_trading_live",
        {"type": "paper_trading_update", "data": {"kind": "position", **PaperPositionSerializer(instance).data}},
        log=logger,
    )


@receiver(post_save, sender=PaperAIDecision)
def broadcast_decision(sender, instance, created, **kwargs):
    if not created:
        return
    broadcast_group(
        "paper_trading_live",
        {"type": "paper_trading_update", "data": {"kind": "decision", **PaperAIDecisionSerializer(instance).data}},
        log=logger,
    )
