"""
Broadcasts to the "risk_live" WebSocket group in two cases:
  1. KillSwitchState.is_active changes (post_save on every save, since
     KillSwitchState is a singleton and every save IS a state change
     worth telling the dashboard about -- there's no "created vs
     updated" distinction that matters here the way there is for
     HistoricalData/TradingSignal).
  2. A new RiskEvent is created with severity=critical -- warning/info
     events are visible in the Risk Log page's REST-fetched list but
     deliberately don't interrupt the live dashboard with a push;
     critical ones do, since those are the ones that actually changed
     what's safe to do right now.

Message shape must keep matching frontend/src/store/liveStore.js's
handleRiskMessage exactly: {"type": "kill_switch", "is_active": bool}
or {"type": <anything else>, ...} for a generic alert.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from common.constants import RiskEventSeverity
from common.websockets import broadcast_group

from .models import AccountEquity, KillSwitchState, RiskEvent

logger = logging.getLogger(__name__)


def _send(data: dict) -> None:
    # Best-effort push to the live dashboard only -- must never raise
    # back into the post_save chain. broadcast_kill_switch_state fires
    # on EVERY KillSwitchState save, so a channel-layer/Redis hiccup
    # here must not be able to block the kill switch itself from
    # persisting as active.
    broadcast_group(
        "risk_live",
        {"type": "risk_alert", "data": data},
        log=logger,
    )


@receiver(post_save, sender=KillSwitchState)
def broadcast_kill_switch_state(sender, instance: KillSwitchState, **kwargs):
    _send({"type": "kill_switch", "is_active": instance.is_active})


@receiver(post_save, sender=RiskEvent)
def broadcast_critical_risk_event(sender, instance: RiskEvent, created: bool, **kwargs):
    if not created or instance.severity != RiskEventSeverity.CRITICAL:
        return
    _send({
        "type": "risk_event",
        "event_type": instance.event_type,
        "symbol": instance.symbol,
        "message": instance.message,
        "severity": instance.severity,
        "created_at": instance.created_at.isoformat(),
    })


@receiver(post_save, sender=AccountEquity)
def record_equity_snapshot(sender, instance: AccountEquity, **kwargs):
    """
    Appends an EquitySnapshot row every time AccountEquity is saved --
    matches every real equity change (apps.execution.paper_executor/
    live_executor's close_position(), and get_equity()'s own
    first-run initialization). Unconditional on every save rather than
    only when current_equity actually changed: AccountEquity is only
    ever saved at meaningful moments already (never on every mark-to-
    market tick -- see paper_executor.mark_to_market's own docstring),
    so there's no meaningful noise to filter out here.
    """
    from django.utils import timezone

    from .models import EquitySnapshot

    EquitySnapshot.objects.create(timestamp=timezone.now(), equity=instance.current_equity)
