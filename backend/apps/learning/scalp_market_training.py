"""Market-candle dataset and inactive candidate. No operational trade rows are written."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import json
import numpy as np
import pandas as pd
from django.utils import timezone
from apps.market_data.models import HistoricalData
from apps.market_data.indicators import _with_indicator_columns, indicator_dict_at
from apps.signals.engine import _indicator_signal_fields
from .scalp_ml_features import BASE_FEATURES, feature_names_for_scalp, vector_for_hypothetical_trade
from .scalp_candidate import split_rows, evaluate


def label_path(entry, risk, future, direction, cost_r=.1, target_r=1.0):
    """Stop-first intrabar policy, adverse gaps at open, positive gaps at target."""
    stop, target = entry - direction * risk, entry + direction * risk * target_r
    exit_price = float(future[-1]["close"])
    exit_index, reason = len(future) - 1, "timeout"
    for i, bar in enumerate(future):
        opening = float(bar["open"])
        if direction * (opening - stop) <= 0:
            exit_price, exit_index, reason = opening, i, "gap_stop"
            break
        stop_hit = float(bar["low"]) <= stop if direction == 1 else float(bar["high"]) >= stop
        target_hit = float(bar["high"]) >= target if direction == 1 else float(bar["low"]) <= target
        if stop_hit:
            exit_price, exit_index, reason = stop, i, "stop"
            break
        if target_hit:
            exit_price, exit_index, reason = target, i, "target"
            break
    net_r = direction * (exit_price - entry) / risk - cost_r
    return net_r, exit_index, reason


def build_market_rows(symbol, limit=60000, horizon=12, cost_r=.1, strategy_setups=False):
    raw = list(HistoricalData.objects.filter(symbol=symbol, timeframe="1m", timestamp__lte=timezone.now()-timedelta(minutes=1))
              .order_by("-timestamp").values("timestamp", "open", "high", "low", "close", "volume", "source")[:limit])
    if len(raw) < 200:
        raise ValueError(f"Insufficient one-minute candles for {symbol}.")
    frame = pd.DataFrame(raw[::-1]).set_index("timestamp")
    for col in ["open", "high", "low", "close", "volume"]:
        frame[col] = frame[col].astype(float)
    valid = ((frame["low"] > 0) & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1)) &
             (frame["low"] <= frame[["open", "close", "high"]].min(axis=1)))
    bad_count = int((~valid).sum())
    frame = _with_indicator_columns(frame.loc[valid].copy())
    from .strategy_methods import SCALPING_METHOD_FUNCS, scalp_idea_from_indicators
    rows = []
    next_allowed = {}
    skipped = 0
    for i in range(60, len(frame)-horizon, 1 if strategy_setups else horizon + 1):
        path = frame.iloc[i:i+horizon+1]
        times = path.index
        if len({timezone.localdate(t.to_pydatetime()) for t in times}) != 1 or any(times[j+1]-times[j] != timedelta(minutes=1) for j in range(len(times)-1)):
            skipped += 1
            continue
        local_time = timezone.localtime(times[0].to_pydatetime()).time()
        if not (local_time.hour * 60 + local_time.minute >= 9*60+15 and local_time.hour * 60 + local_time.minute + horizon < 15*60+30):
            skipped += 1
            continue
        indicators = indicator_dict_at(frame, i)
        fields = _indicator_signal_fields(indicators)
        risk = .6 * indicators["atr"]
        if not np.isfinite(risk) or risk <= 0 or any(fields[f] is None or not np.isfinite(fields[f]) for f in BASE_FEATURES if f != "ind_relative_volume"):
            skipped += 1
            continue
        future = frame.iloc[i+1:i+horizon+1]
        entry = float(future.iloc[0]["open"])
        bars = future.to_dict("records")
        setups = []
        if strategy_setups:
            for method in SCALPING_METHOD_FUNCS:
                if i < next_allowed.get(method, 0):
                    continue
                idea = scalp_idea_from_indicators(method, indicators)
                if idea:
                    setup_risk = entry - idea["stop_loss"]
                    reward = idea["target_price"] - entry
                    if setup_risk > 0 and reward > 0:
                        setups.append((1, method, setup_risk, reward / setup_risk))
                        next_allowed[method] = i + horizon + 1
        else:
            setups = [(1, "market_long", risk, 1.0), (-1, "market_short", risk, 1.0)]
        for direction, method, setup_risk, target_r in setups:
            net_r, exit_index, reason = label_path(entry, setup_risk, bars, direction, cost_r, target_r=target_r)
            rows.append(SimpleNamespace(symbol=symbol, method=method, opened_at=times[1].to_pydatetime(),
                        closed_at=(future.index[exit_index]+timedelta(minutes=1)).to_pydatetime(),
                        pnl=net_r*setup_risk, r_multiple=net_r, exit_reason=reason, entry_price=entry, **fields))
    audit = {"symbol":symbol,"loaded_candles":len(raw),"invalid_candles":bad_count,"skipped_windows":skipped,
             "examples":len(rows),"first_candle":str(frame.index[0]),"last_candle":str(frame.index[-1]),
             "sources":sorted(set(frame["source"]))}
    return rows, audit


def run_market_training(output_dir, symbols=("NIFTY", "BANKNIFTY"), limit=60000, cost_r=.1, threshold=.6, strategy_setups=False):
    from .ml_train import _new_logistic_pipeline, _new_gbm_pipeline
    from .models import ModelRegistry
    import joblib
    rows, audits = [], []
    for symbol in symbols:
        built, audit = build_market_rows(symbol, limit=limit, cost_r=cost_r, strategy_setups=strategy_setups)
        rows.extend(built); audits.append(audit)
    rows.sort(key=lambda row:(row.opened_at, row.symbol, row.method))
    train, validation, test = split_rows(rows)
    if min(len(train),len(validation),len(test)) < 100:
        raise ValueError("Need at least 100 examples per split.")
    unavailable = [f for f in BASE_FEATURES if not any(getattr(r, f) is not None for r in train)]
    features = [f for f in feature_names_for_scalp(sorted({r.method for r in train}), list(symbols)) if f not in unavailable]
    X = [vector_for_hypothetical_trade(r,features) for r in train]
    labels = [int(r.pnl > 0) for r in train]
    if len(set(labels)) != 2:
        raise ValueError("Both classes are required in training.")
    models, validation_metrics = {}, {}
    for name, model in [("logistic",_new_logistic_pipeline()),("gradient_boosting",_new_gbm_pipeline())]:
        model.fit(X,labels); models[name]=model
        validation_metrics[name]=evaluate(model,validation,features,threshold)
    chosen=min(validation_metrics,key=lambda name:validation_metrics[name]["brier_score"])
    test_metrics=evaluate(models[chosen],test,features,threshold)
    prior=sum(labels)/len(labels)
    test_metrics["baseline_brier"]=float(np.mean([(int(r.pnl>0)-prior)**2 for r in test]))
    for metrics in [*validation_metrics.values(),test_metrics]:
        for key in ["all_trades", "selected_trades"]:
            outcomes=metrics[key]
            outcomes["simulated_net_pnl_points"]=outcomes.pop("gross_pnl_points")
            outcomes.pop("mean_r_after_0_1r_cost",None)
    periods={name:{"count":len(data),"days":len({timezone.localdate(r.opened_at) for r in data}),
             "first":str(data[0].opened_at),"last":str(data[-1].opened_at)} for name,data in [("train",train),("validation",validation),("test",test)]}
    per_strategy = {method: evaluate(models[chosen], [r for r in test if r.method == method], features, threshold)
                    for method in sorted({r.method for r in test})}
    for metrics in per_strategy.values():
        for key in ["all_trades", "selected_trades"]:
            outcomes = metrics[key]
            outcomes["simulated_net_pnl_points"] = outcomes.pop("gross_pnl_points")
            outcomes.pop("mean_r_after_0_1r_cost", None)
    report={"strategy_setups":strategy_setups, "per_strategy_test":per_strategy, "status":"inactive_market_candidate","source":"stored real one-minute underlying OHLC candles",
            "chosen_model":chosen,"audits":audits,"sample_count":len(rows),"purged_count":len(rows)-len(train)-len(validation)-len(test),
            "split":periods,"feature_columns":features,"unavailable_features":unavailable,"cost_r":cost_r,
            "horizon_minutes":12,"entry":"next candle open","stop_atr_multiplier":None if strategy_setups else .6,"target_r":None if strategy_setups else 1,
            "validation_models":validation_metrics,"test_metrics":test_metrics,
            "limitations":["Costs are assumed in R, not measured brokerage/spread costs.",
                           "Strategies can overlap in a window; these are correlated examples, not independent portfolio trades." if strategy_setups else "Long and short examples share each window; they are correlated, not independent trades.",
                           "Underlying outcomes are not option-premium execution outcomes.",
                           "Model selected by validation Brier; fixed threshold, untouched test selection.",
                           "No promotion, operational trading rows, or live execution changes."]}
    folder=Path(output_dir)/timezone.now().strftime("%Y%m%d%H%M%S%f");folder.mkdir(parents=True)
    artifact=folder/"model.joblib";joblib.dump(models[chosen],artifact)
    dataset=pd.DataFrame([{**{f:getattr(r,f,None) for f in BASE_FEATURES},"symbol":r.symbol,"direction":r.method, "strategy":r.method,
                         "entry_at":r.opened_at,"exit_at":r.closed_at,"label":int(r.pnl>0),"net_r":r.r_multiple,
                         "exit_reason":r.exit_reason} for r in rows])
    dataset.to_csv(folder/"dataset.csv",index=False)
    registry=ModelRegistry.objects.create(model_name="scalp_strategy_candidate" if strategy_setups else "scalp_market_candidate",model_version=folder.name,
        artifact_path=str(artifact.resolve()),metrics_json=report,active_flag=False)
    report["registry_id"]=registry.pk;report["output_dir"]=str(folder.resolve())
    (folder/"report.json").write_text(json.dumps(report,indent=2))
    return report
