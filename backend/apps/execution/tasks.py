"""
Runs the trading execution cycle: pick up freshly-approved signals and
open positions for them, then check existing open positions for exits.
Routes to apps.execution.paper_executor or apps.execution.live_executor
based on settings.EXECUTION_MODE (NOT settings.BROKER_MODE -- see that
setting's own comment in config/settings.py for why these are
deliberately separate: BROKER_MODE only controls where market DATA
comes from, EXECUTION_MODE controls whether trades are real) -- this
task is the ONE place that decision is made; neither executor module
checks either setting itself.
"""

import logging

from celery import shared_task
from django.conf import settings

from apps.risk.engine import exposure_check_for_execution, is_kill_switch_active
from apps.signals.models import TradingSignal
from common.constants import SignalStatus, SignalType

logger = logging.getLogger(__name__)


@shared_task
def run_trading_cycle(timeframe: str = "5m"):
    """
    Single entry point for both paper and live execution. Checking the
    kill switch again here (on top of it already gating new signal
    approval in apps.risk.engine.check_pre_trade) is deliberate
    defense-in-depth: a signal approved moments before a kill-switch
    trip should still not go on to actually open a position once the
    switch is active -- opening positions and approving signals are
    two separate moments in time, and both need the same guard.
    """
    if is_kill_switch_active():
        logger.warning("run_trading_cycle skipped: kill switch is active.")
        return {"skipped": True, "reason": "kill_switch_active"}

    if settings.EXECUTION_MODE == "paper":
        from .paper_executor import check_and_close_positions, open_position_from_signal
        opener, closer = open_position_from_signal, check_and_close_positions
    elif settings.EXECUTION_MODE == "live":
        from .live_executor import check_and_close_positions_live, open_position_live
        opener, closer = open_position_live, check_and_close_positions_live
    else:
        logger.error("run_trading_cycle: unknown EXECUTION_MODE=%s -- skipping.", settings.EXECUTION_MODE)
        return {"skipped": True, "reason": f"unknown_execution_mode:{settings.EXECUTION_MODE}"}

    opened = []
    skipped_exposure = []
    pending_signals = TradingSignal.objects.filter(
        status=SignalStatus.APPROVED, signal_type__in=[SignalType.BUY, SignalType.SELL],
    )
    for signal in pending_signals:
        # Re-validate exposure right before opening, not just at
        # signal-generation time: check_pre_trade() ran this same check
        # minutes ago, and an earlier signal in THIS batch may have just
        # opened a position in the same symbol (exposure_check_for_execution
        # re-queries OpenPosition fresh on every call, so it sees that).
        exposure_ok, exposure_reason = exposure_check_for_execution(signal.symbol)
        if not exposure_ok:
            logger.warning(
                "Skipping execution of signal %s (%s): %s",
                signal.pk, signal.symbol, exposure_reason,
            )
            skipped_exposure.append({"symbol": signal.symbol, "signal_id": signal.pk, "reason": exposure_reason})
            continue
        try:
            position = opener(signal)
            opened.append({"symbol": signal.symbol, "position_id": position.pk})
        except Exception:
            logger.exception(
                "Failed to open %s position for signal %s", settings.EXECUTION_MODE, signal.pk,
            )

    closed = closer(timeframe)
    return {
        "mode": settings.EXECUTION_MODE, "opened": opened,
        "skipped_exposure": skipped_exposure, "position_updates": closed,
    }


# Backwards-compatible name -- config/celery.py's beat_schedule still
# references this task path; kept as a thin alias so existing schedule
# entries and any external references don't need updating in lockstep
# with this rename from "paper trading cycle" to "trading cycle" (this
# task now branches on BROKER_MODE instead of only ever doing paper
# trading).
run_paper_trading_cycle = run_trading_cycle
