"""
Runs the trading execution cycle: pick up freshly-approved signals and
open positions for them, then check existing open positions for exits.
Routes to apps.execution.paper_executor or apps.execution.live_executor
based on get_execution_mode() (NOT settings.BROKER_MODE -- see that
setting's own comment in config/settings.py for why these are
deliberately separate: BROKER_MODE only controls where market DATA
comes from, EXECUTION_MODE controls whether trades are real) -- this
task is the ONE place that decision is made; neither executor module
checks either setting itself.

get_execution_mode() (apps.execution.models), not settings.EXECUTION_MODE
directly -- the Settings page's Paper/Live toggle (apps.execution.views.
ExecutionModeView) writes to that same DB-backed row, so a change made
there takes effect on this task's very next scheduled run rather than
needing a process restart.
"""

import logging
from datetime import time, timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.risk.engine import enforce_account_risk_limits, is_kill_switch_active
from apps.signals.models import TradingSignal
from common.constants import SignalStatus, SignalType

from .models import BrokerOrder, get_execution_mode

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

    This ONLY blocks opening new positions -- it must never skip
    checking already-open ones. _check_drawdown's DRAWDOWN_FLATTEN_PCT
    case (manual section 13: "20% triggers a full flatten-and-halt")
    trips this same kill switch, and nothing else in this codebase
    closes positions on its own; if this function returned early
    instead of still running the closer, a kill-switch trip -- the
    exact moment risk management matters most -- would leave every
    open position's stop-loss/target/technical-exit checks unmonitored
    indefinitely instead of flattened.
    """
    execution_mode = get_execution_mode()
    live_entry_armed = bool(settings.LIVE_TRADING_ENABLED)
    manage_live_risk = False
    reconciliation = []
    if execution_mode == "paper":
        from .paper_executor import check_and_close_positions, flatten_all_positions, open_position_from_signal
        opener, closer, flattener = open_position_from_signal, check_and_close_positions, flatten_all_positions
    elif execution_mode == "live":
        from .live_executor import (
            check_and_close_positions_live,
            flatten_all_positions_live,
            open_position_live,
            reconcile_broker_orders,
            reconcile_broker_positions,
            sync_live_account_equity,
        )
        opener, closer, flattener = open_position_live, check_and_close_positions_live, flatten_all_positions_live
        live_work_exists = BrokerOrder.objects.filter(status__in=[
            BrokerOrder.Status.CREATED,
            BrokerOrder.Status.SUBMITTED,
            BrokerOrder.Status.UNKNOWN,
        ]).exists()
        from .models import OpenPosition

        live_work_exists = live_work_exists or OpenPosition.objects.filter(
            closed_at__isnull=True, execution_mode="live",
        ).exists()
        # Disarming is an entry gate, not permission to abandon exposure that
        # already exists. Existing orders/positions keep reconciliation and
        # risk-reducing exits enabled; an idle disarmed deployment makes no
        # broker call at all.
        manage_live_risk = live_entry_armed or live_work_exists
        if not manage_live_risk:
            broker_positions_ok = False
            broker_position_reason = "live_trading_disarmed"
            equity_sync = {"synced": False, "reason": broker_position_reason}
        # Resolve any uncertain submission before considering new risk. This
        # never places an order; it only reads the broker order book and
        # applies already-confirmed outcomes from the durable journal.
        else:
            reconciliation = reconcile_broker_orders()
            if not BrokerOrder.objects.filter(status__in=[
                BrokerOrder.Status.CREATED,
                BrokerOrder.Status.SUBMITTED,
                BrokerOrder.Status.UNKNOWN,
            ]).exists():
                try:
                    broker_positions_ok, broker_position_reason = reconcile_broker_positions()
                except Exception as exc:
                    logger.exception("Live broker-position reconciliation failed")
                    broker_positions_ok, broker_position_reason = False, str(exc)
                if broker_positions_ok:
                    try:
                        equity_sync = sync_live_account_equity()
                    except Exception as exc:
                        logger.exception("Live broker-equity synchronization failed")
                        equity_sync = {"synced": False, "reason": str(exc)}
                        broker_positions_ok = False
                        broker_position_reason = "broker_equity_sync_failed"
                else:
                    equity_sync = {"synced": False, "reason": broker_position_reason}
            else:
                broker_positions_ok = False
                broker_position_reason = "unresolved_live_broker_order"
                equity_sync = {"synced": False, "reason": broker_position_reason}
    else:
        logger.error("run_trading_cycle: unknown EXECUTION_MODE=%s -- skipping.", execution_mode)
        return {"skipped": True, "reason": f"unknown_execution_mode:{execution_mode}"}

    from apps.market_data.market_hours import is_market_open

    market_open, market_reason = is_market_open()
    kill_switch_active = is_kill_switch_active()

    # Realize exits before admitting new risk so exposure/equity checks see
    # the freshest state. A kill always chooses the all-position flattener.
    if kill_switch_active:
        logger.warning("run_trading_cycle: kill switch is active -- flattening and blocking entries.")
        closed = flattener(timeframe) if execution_mode == "paper" else flattener()
    elif market_open:
        closed = closer(timeframe)
    else:
        closed = []

    if execution_mode == "live" and manage_live_risk and not BrokerOrder.objects.filter(status__in=[
        BrokerOrder.Status.CREATED,
        BrokerOrder.Status.SUBMITTED,
        BrokerOrder.Status.UNKNOWN,
    ]).exists():
        # A completed exit changes broker cash by fees/taxes as well as gross
        # P&L. Refresh before sizing another entry in the same cycle.
        try:
            equity_sync = sync_live_account_equity()
        except Exception as exc:
            logger.exception("Post-exit broker-equity synchronization failed")
            equity_sync = {"synced": False, "reason": str(exc)}
            broker_positions_ok = False
            broker_position_reason = "broker_equity_sync_failed"

    # A just-realized loss may cross the drawdown threshold. Evaluate account
    # limits now, then flatten immediately in this same cycle if it tripped.
    enforce_account_risk_limits()
    refreshed_kill_state = is_kill_switch_active()
    if refreshed_kill_state and not kill_switch_active:
        closed.extend(flattener(timeframe) if execution_mode == "paper" else flattener())
    kill_switch_active = refreshed_kill_state

    cutoff = timezone.now() - timedelta(
        minutes=getattr(settings, "MAX_SIGNAL_AGE_MINUTES", 10),
    )
    stale_signals = TradingSignal.objects.filter(
        status=SignalStatus.APPROVED, created_at__lt=cutoff,
    )
    for stale in stale_signals.iterator():
        stale.status = SignalStatus.EXPIRED
        stale.reason += " [expired before execution]"
        stale.save(update_fields=["status", "reason"])

    unresolved_live_orders = execution_mode == "live" and BrokerOrder.objects.filter(
        status__in=[
            BrokerOrder.Status.CREATED,
            BrokerOrder.Status.SUBMITTED,
            BrokerOrder.Status.UNKNOWN,
        ],
    ).exists()

    opened = []
    skipped_risk = []
    entries_blocked_reason = None
    if kill_switch_active:
        entries_blocked_reason = "kill_switch_active"
    elif not market_open:
        entries_blocked_reason = f"market_closed:{market_reason}"
    elif execution_mode == "live" and not live_entry_armed:
        entries_blocked_reason = "live_trading_disarmed"
    elif unresolved_live_orders:
        entries_blocked_reason = "unresolved_live_broker_order"
    elif execution_mode == "live" and not broker_positions_ok:
        entries_blocked_reason = f"broker_position_mismatch:{broker_position_reason}"

    if entries_blocked_reason:
        skipped_risk.append({"reason": entries_blocked_reason})
    else:
        pending_signal_ids = list(TradingSignal.objects.filter(
            status=SignalStatus.APPROVED,
            execution_mode=execution_mode,
            signal_type__in=[SignalType.BUY, SignalType.SELL],
        ).values_list("pk", flat=True))
        for signal_id in pending_signal_ids:
            signal = TradingSignal.objects.filter(
                pk=signal_id, status=SignalStatus.APPROVED,
            ).first()
            if signal is None:
                continue
            try:
                if execution_mode == "paper":
                    position = opener(
                        signal, timeframe=timeframe, enforce_execution_risk=True,
                    )
                else:
                    position = opener(signal, timeframe=timeframe)
                opened.append({"symbol": signal.symbol, "position_id": position.pk})
            except Exception as exc:
                logger.exception(
                    "Failed to open %s position for signal %s", execution_mode, signal.pk,
                )
                skipped_risk.append({
                    "symbol": signal.symbol, "signal_id": signal.pk, "reason": str(exc),
                })
                TradingSignal.objects.filter(
                    pk=signal.pk, status=SignalStatus.APPROVED,
                ).update(
                    status=SignalStatus.REJECTED,
                    reason=signal.reason + " [execution failed before submission]",
                )
                if execution_mode == "live":
                    unresolved_after_failure = BrokerOrder.objects.filter(
                        status__in=[
                            BrokerOrder.Status.CREATED,
                            BrokerOrder.Status.SUBMITTED,
                            BrokerOrder.Status.UNKNOWN,
                        ],
                    ).exists()
                    if unresolved_after_failure or is_kill_switch_active():
                        skipped_risk.append({
                            "reason": (
                                "unresolved_live_broker_order"
                                if unresolved_after_failure else "kill_switch_active"
                            ),
                        })
                        break
    post_entry_kill = is_kill_switch_active()
    if post_entry_kill and not kill_switch_active:
        closed.extend(flattener(timeframe) if execution_mode == "paper" else flattener())
    kill_switch_active = post_entry_kill
    return {
        "mode": execution_mode, "opened": opened,
        "skipped_exposure": skipped_risk, "position_updates": closed,
        "kill_switch_active": kill_switch_active,
        "reconciliation": reconciliation,
        "equity_sync": equity_sync if execution_mode == "live" else None,
    }


# Backwards-compatible name -- config/celery.py's beat_schedule still
# references this task path; kept as a thin alias so existing schedule
# entries and any external references don't need updating in lockstep
# with this rename from "paper trading cycle" to "trading cycle" (this
# task now branches on BROKER_MODE instead of only ever doing paper
# trading).
run_paper_trading_cycle = run_trading_cycle


@shared_task
def reconcile_live_broker_orders():
    """Resolve ambiguous live-order journal rows without placing orders."""
    if not BrokerOrder.objects.filter(status__in=[
        BrokerOrder.Status.CREATED,
        BrokerOrder.Status.SUBMITTED,
        BrokerOrder.Status.UNKNOWN,
    ]).exists():
        return {"skipped": True, "reason": "no_pending_live_orders"}
    from .live_executor import reconcile_broker_orders

    return {"reconciled": reconcile_broker_orders()}


@shared_task
def flatten_open_positions():
    """High-priority, idempotent emergency flatten entry point."""
    from .models import OpenPosition
    from .paper_executor import flatten_all_positions

    results = {"paper": flatten_all_positions(), "live": []}
    if OpenPosition.objects.filter(
        closed_at__isnull=True, execution_mode="live",
    ).exists():
        from .live_executor import flatten_all_positions_live

        results["live"] = flatten_all_positions_live()
    return {"mode": get_execution_mode(), "positions": results}


@shared_task
def square_off_intraday_positions():
    """Force all intraday venues flat before the broker's auto-square-off."""
    from apps.market_data.market_hours import NSE_HOLIDAYS

    moment = timezone.localtime()
    if moment.weekday() >= 5 or moment.date() in NSE_HOLIDAYS:
        return {"skipped": True, "reason": "not_a_trading_day"}
    if moment.time() < time(15, 20):
        return {"skipped": True, "reason": "before_square_off_window"}
    return flatten_open_positions()
