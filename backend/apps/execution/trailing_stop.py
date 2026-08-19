"""
Trailing stop-loss. A position opted into trailing (trailing_stop_distance
is set -- see settings.TRAILING_STOP_ENABLED and OpenPosition's own
field docstring) gets its stop ratcheted UP as price makes a new high
since entry, by the same fixed distance the position was originally
sized against. The stop never moves down -- ratcheting is one-directional
by construction (max() below), matching how apps.risk.engine treats a
position's planned risk as fixed once sized; trailing only ever reduces
realized risk from here, never increases it.

Shared by apps.execution.paper_executor and apps.execution.live_executor
(both call update_trailing_stop before their own stop-loss check) so
"when does the stop move" stays identical between paper and live --
only "how the resulting exit order is placed" differs between them,
same principle live_executor's own module docstring already states for
open/close.

SHORT positions trail the mirror-image way: `peak_price` (the field
name is kept as-is -- no migration for a rename) tracks the LOWEST
price seen since entry instead of the highest, and stop_loss ratchets
DOWN toward that trough instead of up toward a peak. Everything else
(one-directional ratcheting, never giving back a locked-in improvement)
is identical in spirit for both sides.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from common.constants import PositionSide


def update_trailing_stop(position, current_price: Decimal) -> bool:
    """
    Updates position.peak_price and, if warranted, ratchets
    position.stop_loss toward it (up for LONG, down for SHORT) --
    saves only the fields that changed. Returns True if stop_loss was
    moved (callers can use this to log or skip re-checking against a
    stop value they already have stale in memory), False if trailing
    is off for this position or price hasn't made a new favorable
    extreme since the last check.
    """
    with transaction.atomic():
        locked = type(position).objects.select_for_update().get(pk=position.pk)
        if locked.closed_at is not None or locked.trailing_stop_distance is None:
            return False

        if locked.side == PositionSide.LONG:
            new_peak = max(locked.peak_price or locked.entry_price, current_price)
            peak_changed = new_peak != locked.peak_price
            new_stop = new_peak - locked.trailing_stop_distance
            stop_changed = new_stop > locked.stop_loss
        else:
            new_peak = min(locked.peak_price or locked.entry_price, current_price)
            peak_changed = new_peak != locked.peak_price
            new_stop = new_peak + locked.trailing_stop_distance
            stop_changed = new_stop < locked.stop_loss

        if not peak_changed and not stop_changed:
            return False

        update_fields = []
        if peak_changed:
            locked.peak_price = new_peak
            update_fields.append("peak_price")
        if stop_changed:
            locked.stop_loss = new_stop
            update_fields.append("stop_loss")
        locked.save(update_fields=update_fields)

    # Keep the caller's instance coherent for the stop comparison that
    # immediately follows this helper.
    position.peak_price = locked.peak_price
    position.stop_loss = locked.stop_loss
    return stop_changed
