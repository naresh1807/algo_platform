"""Train an inactive expected-net-R candidate; never promises winning trades."""
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
from django.utils import timezone


def summarize(frame, mask):
    chosen = frame.loc[mask]
    if chosen.empty:
        return {"count":0,"days":0,"win_rate":None,"mean_net_r":None,"total_net_r":0.0,
                "daily_mean_ci95":None,"coverage":0.0,"profit_factor":None}
    values = chosen.net_r.to_numpy()
    daily = chosen.groupby("day").net_r.mean().to_numpy()
    rng = np.random.default_rng(42)
    bootstrap = rng.choice(daily, size=(1000,len(daily)),replace=True).mean(axis=1)
    gain = values[values>0].sum(); loss = -values[values<0].sum()
    return {"count":len(chosen),"days":len(daily),"win_rate":float((values>0).mean()),
            "mean_net_r":float(values.mean()),"total_net_r":float(values.sum()),
            "daily_mean_ci95":[float(x) for x in np.quantile(bootstrap,[.025,.975])],
            "coverage":len(chosen)/len(frame),"profit_factor":float(gain/loss) if loss>0 else None}


def choose_cutoff(frame, predictions):
    evaluations = []
    for threshold in [0.0,.05,.1,.2,.3]:
        result = summarize(frame, predictions >= threshold)
        eligible = result["count"] >= 50 and result["days"] >= 10 and result["daily_mean_ci95"][0] > 0
        evaluations.append({"threshold_net_r":threshold,"metrics":result,"eligible":eligible})
    acceptable = [r for r in evaluations if r["eligible"]]
    best = max(acceptable,key=lambda r:r["metrics"]["total_net_r"]/len(frame)) if acceptable else None
    return best, evaluations


def train_returns(dataset_path, output_dir, target_description=None, source_limitations=None):
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error
    from .models import ModelRegistry
    import joblib
    path = Path(dataset_path)
    frame = pd.read_csv(path)
    frame["entry_at"] = pd.to_datetime(frame.entry_at, utc=True)
    frame["exit_at"] = pd.to_datetime(frame.exit_at, utc=True)
    frame["day"] = frame.entry_at.dt.tz_convert("Asia/Kolkata").dt.date.astype(str)
    frame = frame.sort_values(["entry_at","symbol","strategy"]).reset_index(drop=True)
    days = sorted(frame.day.unique())
    if len(days)<30:
        raise ValueError("At least thirty market days required.")
    a,b = int(len(days)*.6), int(len(days)*.8)
    va_start = frame.loc[frame.day>=days[a],"entry_at"].min()
    te_start = frame.loc[frame.day>=days[b],"entry_at"].min()
    train = frame[(frame.day<days[a]) & (frame.exit_at<va_start-pd.Timedelta(minutes=12))]
    validation = frame[(frame.day>=days[a]) & (frame.day<days[b]) & (frame.exit_at<te_start-pd.Timedelta(minutes=12))]
    test = frame[frame.day>=days[b]]
    numeric = [c for c in frame if c.startswith("ind_") and frame[c].notna().all()]
    if len(numeric) < 4:
        raise ValueError("At least four complete entry features are required.")
    features = numeric + ["method_"+m for m in sorted(train.strategy.unique())] + ["symbol_"+s for s in sorted(train.symbol.unique())]
    def X(data):
        vectors = data[numeric].copy()
        for col in features[len(numeric):]:
            prefix,value = col.split("_",1)
            vectors[col] = (data["strategy" if prefix=="method" else "symbol"]==value).astype(float)
        return vectors[features].to_numpy()
    if min(len(train),len(validation),len(test))<100 or not np.isfinite(X(frame)).all() or not np.isfinite(frame.net_r).all():
        raise ValueError("Insufficient or non-finite training data.")
    # Bound training-label influence using TRAINING-only quantiles; reporting keeps true outcomes.
    low,high=np.quantile(train.net_r,[.01,.99])
    y=np.clip(train.net_r.to_numpy(),low,high)
    models={"ridge":make_pipeline(StandardScaler(),Ridge(alpha=10)),
            "robust_gradient_boosting":GradientBoostingRegressor(loss="huber",n_estimators=100,max_depth=2,
                                  learning_rate=.05,min_samples_leaf=30,random_state=42)}
    validation_results={}; fitted={}
    for name,model in models.items():
        model.fit(X(train),y); fitted[name]=model
        prediction=model.predict(X(validation))
        cutoff,sweep=choose_cutoff(validation,prediction)
        validation_results[name]={"mae_net_r":float(mean_absolute_error(validation.net_r,prediction)),
                                 "chosen_cutoff":cutoff,"cutoff_evaluations":sweep}
    eligible=[name for name,v in validation_results.items() if v["chosen_cutoff"]]
    if eligible:
        chosen=max(eligible,key=lambda name:validation_results[name]["chosen_cutoff"]["metrics"]["total_net_r"])
        threshold=validation_results[chosen]["chosen_cutoff"]["threshold_net_r"]
    else:
        chosen=min(validation_results,key=lambda name:validation_results[name]["mae_net_r"])
        threshold=None
    prediction=fitted[chosen].predict(X(test))
    selected=np.zeros(len(test),dtype=bool) if threshold is None else prediction>=threshold
    results=summarize(test,selected)
    supported=results["count"]>=50 and results["days"]>=10 and results["daily_mean_ci95"][0]>0
    report={"status":"inactive_return_candidate","target":target_description or "simulated net R including assumed 0.1R cost",
            "chosen_model":chosen,"threshold_net_r":threshold,"selection_supported":supported,
            "selection_reason":"validation positive-return confidence gate passed" if threshold is not None else "no validation cutoff passed positive-return confidence gate",
            "source_dataset":str(path.resolve()),"dataset_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "feature_columns":features,"training_label_clip":[float(low),float(high)],
            "split":{name:{"count":len(data),"days":data.day.nunique(),"first":str(data.entry_at.min()),
                             "last":str(data.entry_at.max())} for name,data in [("train",train),("validation",validation),("test",test)]},
            "validation_models":validation_results,"test":{"mae_net_r":float(mean_absolute_error(test.net_r,prediction)),
            "baseline_all":summarize(test,np.ones(len(test),dtype=bool)),"selected":results},
            "limitations":["Historical test period was previously inspected in earlier model experiments; retrospective evidence only.",
                           "0.1R assumed costs, not actual option fills, brokerage or spreads.",
                           "Day bootstrap is approximate and does not account for all serial dependence or model selection.",
                           "Overlapping strategies are correlated; totals are not executable portfolio returns.",
                           "No automatic activation, execution change, or guarantee of positive trades."]}
    if source_limitations is not None:
        report["limitations"] = source_limitations
    folder=Path(output_dir)/timezone.now().strftime("%Y%m%d%H%M%S%f");folder.mkdir(parents=True)
    artifact=folder/"model.joblib";joblib.dump(fitted[chosen],artifact)
    predictions=test[["entry_at","symbol","strategy","net_r"]].copy()
    predictions["predicted_net_r"]=prediction;predictions["selected"]=selected
    predictions.to_csv(folder/"test_predictions.csv",index=False)
    row=ModelRegistry.objects.create(model_name="scalp_expected_return_candidate",model_version=folder.name,
         artifact_path=str(artifact.resolve()),metrics_json=report,active_flag=False)
    report["registry_id"]=row.pk;report["output_dir"]=str(folder.resolve())
    (folder/"report.json").write_text(json.dumps(report,indent=2))
    return report
