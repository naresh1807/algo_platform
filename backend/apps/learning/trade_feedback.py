"""Append-only feedback from closed executed paper option trades, not hypothetical labels."""
import math
from pathlib import Path
from decimal import Decimal
from celery import shared_task
from django.utils import timezone
from .models import RealizedTradeFeedback, TradeFeedbackRun


def numeric_entry_features(snapshot):
    allowed = {"rsi", "adx", "atr", "atr_pct", "bb_width", "relative_volume", "macd_hist", "macd_hist_pct",
               "ema9", "ema21", "ema50", "ema9_slope", "ema21_slope", "ema9_slope_pct", "ema21_slope_pct",
               "gap_pct", "minutes_since_open", "minutes_to_close", "spread_pct", "iv", "delta", "theta", "vega"}
    result = {}
    for key, value in snapshot.items():
        bare = key.removeprefix("ind_")
        if bare in allowed and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            result["ind_"+bare] = float(value)
    return result


def record_feedback(source, source_id, symbol, strategy, entry_at, exit_at, snapshot, net_pnl, costs, risk):
    if risk <= 0 or exit_at <= entry_at or not all(math.isfinite(float(v)) for v in [risk,net_pnl,costs]):
        return False
    _, created = RealizedTradeFeedback.objects.get_or_create(source=source, source_id=source_id,
          defaults={"environment":"paper","symbol":symbol,"strategy":strategy,"entry_at":entry_at,"exit_at":exit_at,
                    "feature_snapshot":numeric_entry_features(snapshot),"net_pnl":net_pnl,"costs":costs,
                    "initial_risk":risk,"net_r":float(net_pnl/risk),"profitable":net_pnl>0})
    return created


@shared_task(name="apps.learning.trade_feedback.collect_trade_feedback")
def collect_trade_feedback():
    from apps.paper_trading.models import PaperTrade, PaperAIDecision
    from apps.execution.models import OpenPosition
    created=0; skipped=0
    trades=PaperTrade.objects.exclude(id__in=RealizedTradeFeedback.objects.filter(source="paper_ai_trade").values("source_id")).select_related("position","position__contract")
    for trade in trades:
        position=trade.position
        decision=PaperAIDecision.objects.filter(decision_id=position.entry_decision_id,is_hypothetical=False,source_environment="PAPER").first()
        if decision is None or decision.timestamp > position.opened_at:
            skipped+=1;continue
        risk=abs(trade.entry_price-position.initial_stop_loss)*trade.quantity
        ok=record_feedback("paper_ai_trade",trade.pk,decision.symbol,"paper_ai_"+position.contract.option_type,
             position.opened_at,trade.closed_at,decision.feature_snapshot_json,trade.net_pnl,
             max(Decimal(0),trade.gross_pnl-trade.net_pnl),risk)
        created+=int(ok);skipped+=int(not ok)
    positions=OpenPosition.objects.filter(execution_mode="paper",closed_at__isnull=False,realized_pnl__isnull=False,option_contract__isnull=False).exclude(id__in=RealizedTradeFeedback.objects.filter(source="execution_option").values("source_id")).select_related("signal","option_contract")
    for position in positions:
        signal=position.signal
        if signal is None or signal.created_at > position.opened_at:
            skipped+=1;continue
        snapshot={key:getattr(signal,key) for key in ["ind_rsi","ind_adx","ind_atr_pct","ind_bb_width","ind_relative_volume","ind_macd_hist_pct","ind_ema9_slope_pct","ind_ema21_slope_pct"]}
        risk=abs(position.entry_price-signal.stop_loss)*position.qty
        ok=record_feedback("execution_option",position.pk,signal.symbol,"option_"+position.option_contract.option_type,
             position.opened_at,position.closed_at,snapshot,position.realized_pnl,position.total_costs,risk)
        created+=int(ok);skipped+=int(not ok)
    report={"created":created,"skipped_missing_entry_or_risk":skipped,"sample_count":RealizedTradeFeedback.objects.count()}
    if created:TradeFeedbackRun.objects.create(kind="collect",report=report)
    return report


@shared_task(name="apps.learning.trade_feedback.retrain_from_taken_trades")
def retrain_from_taken_trades():
    from django.conf import settings
    import pandas as pd
    from .scalp_return_training import train_returns
    collect_trade_feedback()
    records=list(RealizedTradeFeedback.objects.filter(environment="paper").order_by("entry_at"))
    days=len({timezone.localdate(row.entry_at) for row in records})
    if len(records)<100 or days<30:
        report={"trained":False,"reason":"insufficient_taken_trade_history","sample_count":len(records),"days":days,
                "required_samples":100,"required_days":30}
        TradeFeedbackRun.objects.create(kind="train",report=report)
        return report
    folder=Path(settings.BASE_DIR)/"ml_artifacts"/"taken_trade_learning"/timezone.now().strftime("%Y%m%d%H%M%S%f")
    folder.mkdir(parents=True)
    data=[{**row.feature_snapshot,"entry_at":row.entry_at,"exit_at":row.exit_at,"symbol":row.symbol,
           "strategy":row.strategy,"net_r":row.net_r,"trade_ref":f"{row.source}:{row.source_id}"} for row in records]
    dataset=folder/"dataset.csv";pd.DataFrame(data).to_csv(dataset,index=False)
    try:
        report=train_returns(dataset,folder,target_description="Realized paper-option net R after recorded modeled costs",
                             source_limitations=["Executed paper fills and costs remain simulated, not live broker results.",
                                 "Taken trades reflect past strategy selection and omit rejected opportunities.",
                                 "Candidate is inactive; forward paper evaluation is required before execution use."])
        report["trained"]=True
    except ValueError as error:
        report={"trained":False,"reason":str(error),"sample_count":len(records)}
    TradeFeedbackRun.objects.create(kind="train",report=report)
    return report
