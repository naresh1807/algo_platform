"""
DailyLearningService -- the end-of-day pipeline (spec's 12-step
sequence): finalize outcomes, label taken/skipped/rejected decisions
(including hypothetical HOLD/SKIP outcomes via outcome_service's
walk-forward), build the day's apps.learning.TrainingSample rows,
train+evaluate+promote-or-reject a challenger, and produce the
PaperDailySummary report.

Trade outcomes (finalize_trade_outcome) are already computed
synchronously at close time by position_service.close_position -- this
service's job is everything that can only happen once the WHOLE day's
data is in: hypothetical-decision labeling (needs forward candles that
may not exist yet mid-session) and the daily model retrain.
"""

from __future__ import annotations

from decimal import Decimal


def label_hypothetical_decisions(trading_day, timeframe: str = "5m", horizon_bars: int = 12) -> int:
    from . import outcome_service
    from ..models import PaperAIDecision

    labeled = 0
    decisions = PaperAIDecision.objects.filter(
        timestamp__date=trading_day, is_hypothetical=True, hypothetical_outcome_json__isnull=True,
    )
    for decision in decisions:
        entry_ref = float((decision.feature_snapshot_json or {}).get("close") or 0)
        outcome = outcome_service.compute_hypothetical_outcome(
            decision.symbol, timeframe, decision.timestamp, entry_ref, horizon_bars,
        )
        decision.hypothetical_outcome_json = outcome
        if outcome.get("available"):
            # No real risk was ever defined for a non-trade, so this is a
            # documented proxy normalization (move_pct / 10), not a real
            # R-multiple -- kept in the same net_r field so the training
            # dataset builder can treat traded and hypothetical rows
            # uniformly.
            decision.net_r = round(outcome["move_pct"] / 10.0, 4)
        decision.save(update_fields=["hypothetical_outcome_json", "net_r"])
        labeled += 1
    return labeled


def build_training_samples_for_day(trading_day, model_name: str = "paper_trading_policy") -> int:
    from apps.learning.models import TrainingSample

    from ..models import PaperAIDecision

    created = 0
    for decision in PaperAIDecision.objects.filter(timestamp__date=trading_day):
        if decision.is_hypothetical:
            outcome = decision.hypothetical_outcome_json or {}
            if not outcome.get("available"):
                continue
            # A HOLD/SKIP was "correct" (label=1) if the underlying didn't
            # move meaningfully either way -- nothing worth trading was
            # actually missed. This is the mechanism that lets the model
            # learn when skipping was right, not just when trading was.
            label = 1 if abs(outcome.get("move_pct", 0)) < 0.3 else 0
            net_r = decision.net_r
        else:
            if decision.trade_id is None or decision.net_r is None:
                continue
            label = 1 if decision.net_r > 0 else 0
            net_r = float(decision.net_r)

        TrainingSample.objects.create(
            source_environment=TrainingSample.SourceEnvironment.PAPER, model_name=model_name,
            decision_ref=str(decision.decision_id), symbol=decision.symbol, timestamp=decision.timestamp,
            feature_schema_version=decision.feature_schema_version, feature_vector_json=decision.feature_snapshot_json,
            label=label, net_r=net_r, is_hypothetical=decision.is_hypothetical,
        )
        created += 1
    return created


def run_end_of_day(trading_day=None) -> "PaperDailySummary":  # noqa: F821
    from django.utils import timezone

    from . import model_evaluation_service, model_promotion_service, model_training_service, paper_risk_engine
    from ..models import PaperAccount, PaperDailySummary, PaperTrade

    trading_day = trading_day or timezone.localdate()

    label_hypothetical_decisions(trading_day)
    samples_created = build_training_samples_for_day(trading_day)

    account = PaperAccount.objects.get(pk=1)
    trades = list(PaperTrade.objects.filter(closed_at__date=trading_day))
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_win = sum((t.net_pnl for t in wins), Decimal(0))
    gross_loss = abs(sum((t.net_pnl for t in losses), Decimal(0)))
    total_charges = sum((t.gross_pnl - t.net_pnl for t in trades), Decimal(0))
    total_holding_seconds = sum((t.result.time_in_trade_seconds for t in trades if hasattr(t, "result")), 0)
    avg_mfe = (
        sum((t.result.mfe for t in trades if hasattr(t, "result")), Decimal(0)) / len(trades) if trades else None
    )
    avg_mae = (
        sum((t.result.mae for t in trades if hasattr(t, "result")), Decimal(0)) / len(trades) if trades else None
    )

    champion = model_promotion_service.current_champion("paper_trading_policy")
    champion_version = champion.model_version if champion else ""

    training_result = model_training_service.train_challenger()
    if training_result.get("trained"):
        performance = model_evaluation_service.evaluate_holdout_performance(training_result["holdout_rows"])
        regimes = model_evaluation_service.regimes_represented(training_result["holdout_rows"])
        promotion = model_promotion_service.promote_or_reject(
            model_name="paper_trading_policy", ensemble=training_result["ensemble"],
            metrics=training_result["metrics"], performance=performance, regimes=regimes,
        )
        challenger_result = {"trained": True, **training_result["metrics"], "performance": performance}
        promotion_status = "promoted" if promotion["promoted"] else f"rejected: {promotion['reason']}"
        if promotion["promoted"]:
            champion_version = promotion["model_version"]
    else:
        challenger_result = training_result
        promotion_status = f"not_trained: {training_result.get('reason', '')}"

    summary, _ = PaperDailySummary.objects.update_or_create(
        trading_day=trading_day,
        defaults=dict(
            opening_equity=account.daily_start_equity or account.current_equity,
            closing_equity=account.current_equity,
            trades_count=len(trades), wins=len(wins), losses=len(losses),
            gross_pnl=sum((t.gross_pnl for t in trades), Decimal(0)),
            total_charges=total_charges,
            net_pnl=sum((t.net_pnl for t in trades), Decimal(0)),
            win_rate=(Decimal(len(wins)) / Decimal(len(trades))) if trades else None,
            profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
            max_drawdown=account.daily_drawdown,
            consecutive_losses=paper_risk_engine.consecutive_losses(account),
            avg_holding_seconds=(total_holding_seconds // len(trades)) if trades else None,
            avg_mfe=avg_mfe, avg_mae=avg_mae,
            champion_model_version=champion_version,
            challenger_result_json=challenger_result,
            promotion_status=promotion_status,
            report_json={"samples_created": samples_created, "trades": len(trades)},
        ),
    )
    return summary
