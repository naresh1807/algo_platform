"""
The one function that pushes a JARVIS announcement (manual 14.16) to
every connected dashboard. Delegates entirely to common.websockets.
broadcast_group -- same best-effort, log-only, single-persistent-
connection broadcast every other Channels group_send in this codebase
now uses (see that helper's own module-level comment), since a
broadcast failure must never block the real event (a kill-switch trip,
a closed trade) that triggered it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def announce(kind: str, message: str, **extra) -> None:
    from common.websockets import broadcast_group

    broadcast_group(
        "jarvis_live",
        {
            "type": "jarvis_announcement",
            "data": {"kind": kind, "message": message, **extra},
        },
        log=logger,
    )
