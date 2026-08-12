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
"""

from __future__ import annotations

from decimal import Decimal


def update_trailing_stop(position, current_price: Decimal) -> bool:
    """
    Updates position.peak_price and, if warranted, ratchets
    position.stop_loss up -- saves only the fields that changed.
    Returns True if stop_loss was moved (callers can use this to log
    or skip re-checking against a stop value they already have stale
    in memory), False if trailing is off for this position or price
    hasn't made a new high since the last check.
    """
    if position.trailing_stop_distance is None:
        return False

    new_peak = max(position.peak_price or position.entry_price, current_price)
    peak_changed = new_peak != position.peak_price

    new_stop = new_peak - position.trailing_stop_distance
    stop_changed = new_stop > position.stop_loss

    if not peak_changed and not stop_changed:
        return False

    update_fields = []
    if peak_changed:
        position.peak_price = new_peak
        update_fields.append("peak_price")
    if stop_changed:
        position.stop_loss = new_stop
        update_fields.append("stop_loss")

    position.save(update_fields=update_fields)
    return stop_changed
