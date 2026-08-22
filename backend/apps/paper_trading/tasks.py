"""
Autonomous paper-trading Celery tasks. run_paper_trading_cycle is the
main loop tick (mirrors apps.execution.tasks.run_trading_cycle's
market-hours gate and single-entry-point shape, but drives this app's
OWN state machine/tables -- never apps.execution's). AI decisions occur
once per completed candle (spec: "AI decisions should preferably occur
on completed candles, not repeatedly on every incomplete candle") --
the beat schedule fires this once per `timeframe` bar, and
market_data_reader/feature_engineering already drop the still-forming
candle regardless of how often this task is invoked.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.market_data import market_hours

from .models import Action, PaperAIDecision, PaperPosition, get_or_create_account
from .services import (
    ai_signal_service, exit_manager, execution_engine, feature_engineering,
    market_data_reader, paper_risk_engine, position_service, state_machine,
)

logger = logging.getLogger(__name__)


def _default_underlying() -> str:
    watchlist = getattr(settings, "OPTIONS_PIPELINE_UNDERLYINGS", None) or settings.WATCHLIST
    return watchlist[0] if watchlist else "NIFTY"


def _record_decision(result: ai_signal_service.AIDecisionResult, *, symbol: str, contract=None, is_hypothetical: bool = True, risk_response: dict | None = None) -> PaperAIDecision:
    return PaperAIDecision.objects.create(
        decision_id=uuid.uuid4(), timestamp=timezone.now(), symbol=symbol, contract=contract,
        feature_snapshot_json=result.features or {}, feature_schema_version=feature_engineering.FEATURE_SCHEMA_VERSION,
        model_version=result.model_version, action=result.action, confidence=result.confidence,
        expected_return=result.expected_return, selected_stop=result.selected_stop,
        risk_engine_response_json=risk_response or {}, is_hypothetical=is_hypothetical,
        explanation_json=result.explanation,
    )


def _manage_open_position(account, position: PaperPosition, timeframe: str) -> dict:
    state_machine.transition(state_machine.PaperSessionState.State.POSITION_MANAGEMENT, reason="managing open position")

    quote = market_data_reader.latest_contract_quote(position.contract)
    if quote is None or quote.ltp is None:
        return {"action": "no_quote_available"}

    mark_source = quote.bid if quote.bid is not None else quote.ltp
    mark = Decimal(str(mark_source))
    position_service.mark_to_market(position, mark)

    exit_reason, detail = exit_manager.evaluate_hard_exit(position, quote)
    if exit_reason:
        state_machine.transition(state_machine.PaperSessionState.State.EXIT_PENDING, reason=exit_reason)
        try:
            trade = position_service.close_position(account, position, exit_reason)
        except execution_engine.OrderRejected as exc:
            logger.error("run_paper_trading_cycle: exit order rejected for position=%s: %s", position.pk, exc)
            paper_risk_engine.record_risk_event("exit_order_rejected", str(exc), severity="critical")
            state_machine.transition(state_machine.PaperSessionState.State.ERROR_SAFE_MODE, reason=str(exc))
            return {"action": "exit_failed", "reason": str(exc)}
        state_machine.transition(state_machine.PaperSessionState.State.POSITION_CLOSED, reason=exit_reason)
        if paper_risk_engine.consecutive_losses_cooldown_active(account):
            state_machine.transition(state_machine.PaperSessionState.State.COOLDOWN, reason="consecutive-loss limit reached")
        else:
            state_machine.transition(state_machine.PaperSessionState.State.FLAT, reason="ready for next signal")
        return {"action": "exited", "reason": exit_reason, "net_pnl": str(trade.net_pnl)}

    # AI stop management: trail once meaningfully in profit, otherwise keep.
    features = feature_engineering.build_underlying_features(position.contract.underlying, timeframe) or {}
    atr = features.get("atr")
    in_profit_r = (
        (mark - position.average_entry_price) / (position.average_entry_price - position.initial_stop_loss)
        if position.average_entry_price != position.initial_stop_loss else Decimal(0)
    )
    if position.trailing_stop_distance is None and in_profit_r >= 1:
        position.trailing_stop_distance = position.average_entry_price - position.initial_stop_loss
        position.save(update_fields=["trailing_stop_distance"])
    chosen_action = Action.TRAIL_STOP if position.trailing_stop_distance is not None else Action.KEEP_CURRENT_STOP
    exit_manager.apply_stop_action(position, chosen_action, mark_price=mark, atr=Decimal(str(atr)) if atr else None)

    PaperAIDecision.objects.create(
        decision_id=uuid.uuid4(), timestamp=timezone.now(), symbol=position.contract.underlying, contract=position.contract,
        feature_snapshot_json=features, feature_schema_version=feature_engineering.FEATURE_SCHEMA_VERSION,
        action=chosen_action, is_hypothetical=False, explanation_json={"method": "position_management", "positive_factors": [], "negative_factors": []},
    )
    return {"action": "held_position", "stop_action": chosen_action}


def _run_cycle(underlying: str | None, timeframe: str, evaluate_fn) -> dict:
    """Shared body for run_paper_trading_cycle (swing, 5m,
    ai_signal_service.evaluate_entry) and run_paper_trading_scalp_cycle
    (scalp, 1m, ai_signal_service.evaluate_scalp_entry) -- market-hours/
    cooldown/pending-order checks, open-position management, and entry
    submission are identical between the two modes; only WHICH function
    proposes the candidate entry differs. Both modes share the same
    account, state machine, and one-open-position slot -- whichever
    fires first (swing or scalp) holds it until it closes, the same
    "natural, bounded throttle" apps.learning.scalp_execution's own
    module docstring already documents for its equivalent competition
    with apps.options.index_direction_strategy in the OTHER (legacy)
    execution pipeline.
    """
    underlying = underlying or _default_underlying()
    account = get_or_create_account()

    market_open, reason = market_hours.is_market_open()
    if not market_open:
        state_machine.transition(state_machine.PaperSessionState.State.MARKET_CLOSED, reason=reason)
        return {"skipped": "market_closed", "reason": reason}

    session = state_machine.current_state()
    if session.state == session.State.COOLDOWN:
        if not state_machine.cooldown_expired():
            return {"skipped": "cooldown"}
        state_machine.transition(session.State.FLAT, reason="cooldown expired")

    execution_engine.check_pending_limit_orders()

    open_position = PaperPosition.objects.filter(account=account, status=PaperPosition.Status.OPEN).first()
    if open_position is not None:
        return _manage_open_position(account, open_position, timeframe)

    state_machine.transition(state_machine.PaperSessionState.State.ANALYZING, reason="scanning for entry")
    result = evaluate_fn(account, underlying)

    if result.action == Action.HOLD:
        _record_decision(result, symbol=underlying)
        state_machine.transition(state_machine.PaperSessionState.State.FLAT, reason="hold")
        return {"action": "hold", "reason": result.reason}

    if result.action == Action.SKIP_SIGNAL:
        contract = result.contract_candidate.contract if result.contract_candidate else None
        _record_decision(result, symbol=underlying, contract=contract, risk_response={"reason": result.reason})
        state_machine.transition(state_machine.PaperSessionState.State.FLAT, reason="skip")
        return {"action": "skip", "reason": result.reason}

    # BUY_CE_ONE_LOT / BUY_PE_ONE_LOT
    candidate = result.contract_candidate
    state_machine.transition(state_machine.PaperSessionState.State.ENTRY_CANDIDATE, reason=result.reason)
    state_machine.transition(state_machine.PaperSessionState.State.ORDER_PENDING, reason="submitting entry order")

    quantity = candidate.contract.lot_size * settings.LOTS_PER_TRADE
    try:
        order = execution_engine.submit_entry_order(account, candidate.contract, quantity)
    except execution_engine.OrderRejected as exc:
        _record_decision(result, symbol=underlying, contract=candidate.contract, risk_response={"reason": str(exc)})
        state_machine.transition(state_machine.PaperSessionState.State.FLAT, reason=f"order rejected: {exc}")
        return {"action": "order_rejected", "reason": str(exc)}

    decision = _record_decision(result, symbol=underlying, contract=candidate.contract, is_hypothetical=False)
    position = position_service.open_position(
        account, candidate.contract, order,
        stop_loss=result.selected_stop, target_price=result.selected_target,
        entry_decision_id=decision.decision_id,
    )
    state_machine.transition(state_machine.PaperSessionState.State.POSITION_OPEN, reason="entered")
    return {"action": "entered", "position_id": position.pk, "contract": str(candidate.contract)}


@shared_task
def run_paper_trading_cycle(underlying: str | None = None, timeframe: str = "5m") -> dict:
    return _run_cycle(underlying, timeframe, lambda account, u: ai_signal_service.evaluate_entry(account, u, timeframe))


@shared_task
def run_paper_trading_scalp_cycle(underlying: str | None = None) -> dict:
    """Scalp-mode sibling of run_paper_trading_cycle -- own 1-minute beat
    schedule entry (matching apps.learning.tasks.run_scalping_real_execution's
    own cadence: checking a scalp's stop/target only every 5 minutes would
    defeat the point of scalping). Sources entries from
    ai_signal_service.evaluate_scalp_entry (apps.learning.strategy_methods.
    SCALPING_METHOD_FUNCS -- the same SAR+volume-burst/EMA-momentum/
    RSI-extreme idea generators apps.learning.scalp_execution already
    trades for real in the OTHER, legacy execution pipeline) instead of
    the swing task's 5m weighted heuristic. Fully additive: same account,
    same one-open-position slot, same state machine, same risk engine,
    same ledger, same hard risk boundaries -- see _run_cycle's own
    docstring for how it competes with the swing cycle for that slot.
    """
    return _run_cycle(underlying, "1m", lambda account, u: ai_signal_service.evaluate_scalp_entry(account, u))


@shared_task
def paper_square_off_intraday() -> dict:
    """Force-flat any open paper position before the configured
    intraday cutoff -- mirrors apps.execution.tasks.
    square_off_intraday_positions's role for this app's own tables."""
    account = get_or_create_account()
    position = PaperPosition.objects.filter(account=account, status=PaperPosition.Status.OPEN).first()
    if position is None:
        return {"squared_off": False}
    trade = position_service.close_position(account, position, "intraday_square_off")
    state_machine.transition(state_machine.PaperSessionState.State.FLAT, reason="intraday square-off")
    return {"squared_off": True, "net_pnl": str(trade.net_pnl)}


@shared_task
def generate_daily_learning_report(trading_day: str | None = None) -> dict:
    from datetime import date

    from .services import daily_learning_service

    day = date.fromisoformat(trading_day) if trading_day else None
    summary = daily_learning_service.run_end_of_day(day)
    return {"trading_day": str(summary.trading_day), "net_pnl": str(summary.net_pnl), "promotion_status": summary.promotion_status}
