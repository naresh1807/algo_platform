"""Offline, day-separated scalp training. Never promotes or changes execution."""
from datetime import timedelta
from pathlib import Path
import json
from django.utils import timezone
from .scalp_ml_features import BASE_FEATURES, feature_names_for_scalp, vector_for_hypothetical_trade


def split_rows(rows):
    days = sorted({timezone.localdate(row.opened_at) for row in rows})
    if len(days) < 5:
        raise ValueError("At least five distinct trading days are needed for day-separated evaluation.")
    first = max(1, int(len(days) * .6))
    second = max(first + 1, int(len(days) * .8))
    validation_day, test_day = days[first], days[second]
    validation_start = min(row.opened_at for row in rows if timezone.localdate(row.opened_at) >= validation_day)
    test_start = min(row.opened_at for row in rows if timezone.localdate(row.opened_at) >= test_day)
    embargo = timedelta(minutes=12)
    train = [r for r in rows if timezone.localdate(r.opened_at) < validation_day and r.closed_at < validation_start - embargo]
    validation = [r for r in rows if validation_day <= timezone.localdate(r.opened_at) < test_day and r.closed_at < test_start - embargo]
    test = [r for r in rows if timezone.localdate(r.opened_at) >= test_day]
    return train, validation, test


def evaluate(model, rows, features, threshold):
    from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score
    y = [int(r.pnl > 0) for r in rows]
    probabilities = model.predict_proba([vector_for_hypothetical_trade(r, features) for r in rows])[:, 1]
    selected = [r for r, probability in zip(rows, probabilities) if probability >= threshold]
    def outcomes(data):
        multiples = [float(r.r_multiple) for r in data if r.r_multiple is not None]
        return {"count": len(data), "win_rate": sum(r.pnl > 0 for r in data) / len(data) if data else None,
                "mean_r": sum(multiples) / len(multiples) if multiples else None, "r_count": len(multiples),
                "gross_pnl_points": float(sum(r.pnl for r in data)),
                "mean_r_after_0_1r_cost": sum(r - .1 for r in multiples) / len(multiples) if multiples else None}
    return {"count": len(rows), "accuracy": float(accuracy_score(y, probabilities >= .5)),
            "brier_score": float(brier_score_loss(y, probabilities)),
            "roc_auc": float(roc_auc_score(y, probabilities)) if len(set(y)) == 2 else None,
            "baseline_brier": None, "all_trades": outcomes(rows), "selected_trades": outcomes(selected),
            "coverage": len(selected) / len(rows), "threshold": threshold}


def train_candidate(output_dir, threshold=.6):
    import joblib
    import numpy as np
    from .models import HypotheticalTrade, ModelRegistry
    from .strategy_methods import SCALPING_METHOD_FUNCS
    from .ml_train import _new_logistic_pipeline, _new_gbm_pipeline
    candidates = list(HypotheticalTrade.objects.filter(method__in=SCALPING_METHOD_FUNCS, closed_at__isnull=False,
                           pnl__isnull=False).order_by("opened_at", "id"))
    usable_features = [f for f in BASE_FEATURES if any(getattr(r, f, None) is not None for r in candidates)]
    unavailable_features = [f for f in BASE_FEATURES if f not in usable_features]
    if len(usable_features) < 4:
        raise ValueError("Fewer than four recorded entry indicators are available.")
    rows = [r for r in candidates if all(getattr(r, f, None) is not None and np.isfinite(getattr(r, f)) for f in usable_features)]
    train, validation, test = split_rows(rows)
    for name, data, minimum in [("training", train, 30), ("validation", validation, 15), ("test", test, 15)]:
        if len(data) < minimum:
            raise ValueError(f"Insufficient {name} samples: {len(data)} (need {minimum}).")
    labels = [int(r.pnl > 0) for r in train]
    if len(set(labels)) < 2:
        raise ValueError("Training data needs wins and losses.")
    features = [f for f in feature_names_for_scalp(sorted(SCALPING_METHOD_FUNCS), sorted({r.symbol for r in rows})) if f not in unavailable_features]
    X = [vector_for_hypothetical_trade(r, features) for r in train]
    fitted, validation_metrics = {}, {}
    for name, model in [("logistic", _new_logistic_pipeline()), ("gradient_boosting", _new_gbm_pipeline())]:
        model.fit(X, labels)
        fitted[name] = model
        validation_metrics[name] = evaluate(model, validation, features, threshold)
    winner = min(validation_metrics, key=lambda name: validation_metrics[name]["brier_score"])
    model = fitted[winner]
    test_metrics = evaluate(model, test, features, threshold)
    prior = sum(labels) / len(labels)
    test_metrics["baseline_brier"] = float(np.mean([(int(r.pnl > 0) - prior) ** 2 for r in test]))
    def period(data):
        return {"count": len(data), "first": data[0].opened_at.isoformat(), "last": data[-1].opened_at.isoformat(),
                "days": len({timezone.localdate(r.opened_at) for r in data})}
    report = {"status": "candidate_only", "chosen_model": winner, "active": False,
              "source": "closed hypothetical underlying scalps; not real option premium fills",
              "unavailable_features": unavailable_features, "feature_columns": features,
              "raw_count": len(candidates), "complete_feature_count": len(rows), "excluded_missing_features": len(candidates)-len(rows),
              "excluded_overlap_count": len(rows)-len(train)-len(validation)-len(test),
              "split": {"train": period(train), "validation": period(validation), "test": period(test)},
              "validation_models": validation_metrics, "test_metrics": test_metrics,
              "limitations": ["Only nine or fewer observed trading days; results may not generalize.",
                              "Labels use gross underlying comparison P&L, excluding broker charges and option spreads.",
                              "0.1R cost sensitivity is illustrative, not measured execution costs.",
                              "Threshold is predeclared, model selected on validation Brier, test never used to select.",
                              "Candidate fitted on training days only; no automatic promotion or trade execution."]}
    folder = Path(output_dir); folder.mkdir(parents=True, exist_ok=True)
    artifact = folder / "scalp_candidate.joblib"
    joblib.dump(model, artifact)
    row = ModelRegistry.objects.create(model_name="scalp_selection_candidate", model_version=timezone.now().strftime("%Y%m%d%H%M%S%f"),
                 artifact_path=str(artifact.resolve()), metrics_json={**report, "feature_columns": features}, active_flag=False)
    report["registry_id"] = row.pk
    (folder / "report.json").write_text(json.dumps(report, indent=2))
    return report
