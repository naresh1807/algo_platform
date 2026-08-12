"""
The one function that pushes a JARVIS announcement (manual 14.16) to
every connected dashboard. Mirrors apps.risk.signals' use of
channel_layer.group_send -- same defensive try/except-and-log-only
pattern as apps.admin_tools.audit.log_action, since a broadcast failure
must never block the real event (a kill-switch trip, a closed trade)
that triggered it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def announce(kind: str, message: str, **extra) -> None:
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            "jarvis_live",
            {
                "type": "jarvis_announcement",
                "data": {"kind": kind, "message": message, **extra},
            },
        )
    except Exception:
        logger.exception("Failed to broadcast JARVIS announcement kind=%s", kind)
