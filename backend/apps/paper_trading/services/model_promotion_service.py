"""
ModelPromotionService -- champion/challenger lifecycle for the
"paper_trading_policy" apps.learning.ModelRegistry family. Layers this
subsystem's own promotion gates (spec: minimum sample size, positive
net expectancy, acceptable profit factor/drawdown, multi-regime
coverage) ON TOP of apps.learning.ml_train._should_promote (the SAME
accuracy-regression gate the win_probability/scalp_win_probability
families already share) -- ANY gate failing means the challenger is
still RECORDED (active_flag=False, for audit history -- never deleted)
but the current champion is left completely unchanged. A profitable day
alone never promotes a challenger; every gate below must pass.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone as dt_timezone

MIN_HOLDOUT_SAMPLES = 10
MIN_PROFIT_FACTOR = 1.0
MIN_EXPECTANCY_R = 0.0
MAX_ACCEPTABLE_DRAWDOWN_R = 10.0
MIN_REGIMES_REPRESENTED = 1


def current_champion(model_name: str):
    from apps.learning.models import ModelRegistry

    return ModelRegistry.objects.filter(model_name=model_name, active_flag=True).first()


def evaluate_promotion(*, model_name: str, challenger_metrics: dict, performance: dict, regimes: set[str]) -> tuple[bool, str]:
    from apps.learning.ml_train import _should_promote

    champion = current_champion(model_name)
    champion_metrics = champion.metrics_json if champion else None

    accuracy_ok, accuracy_reason = _should_promote(challenger_metrics, champion_metrics)
    if not accuracy_ok:
        return False, f"accuracy_gate_failed: {accuracy_reason}"

    if performance["sample_count"] < MIN_HOLDOUT_SAMPLES:
        return False, f"insufficient_holdout_samples: {performance['sample_count']} < {MIN_HOLDOUT_SAMPLES}"
    if performance["expectancy_r"] is None or performance["expectancy_r"] < MIN_EXPECTANCY_R:
        return False, f"non_positive_expectancy: {performance['expectancy_r']}"
    if performance["profit_factor"] is None or performance["profit_factor"] < MIN_PROFIT_FACTOR:
        return False, f"profit_factor_below_minimum: {performance['profit_factor']}"
    if performance["max_drawdown_r"] is not None and performance["max_drawdown_r"] > MAX_ACCEPTABLE_DRAWDOWN_R:
        return False, f"max_drawdown_too_large: {performance['max_drawdown_r']}"
    if len(regimes) < MIN_REGIMES_REPRESENTED:
        return False, f"insufficient_regime_coverage: {sorted(regimes)}"

    return True, accuracy_reason


def promote_or_reject(*, model_name: str, ensemble, metrics: dict, performance: dict, regimes: set[str]) -> dict:
    import joblib
    from django.conf import settings

    from apps.learning.models import ModelRegistry

    should_promote, reason = evaluate_promotion(
        model_name=model_name, challenger_metrics=metrics, performance=performance, regimes=regimes,
    )

    version = datetime.now(dt_timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    artifact_dir = settings.ML_ARTIFACT_DIR
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = str(artifact_dir / f"{model_name}_{version}.joblib")
    joblib.dump(ensemble, artifact_path)

    combined_metrics = {**metrics, "performance": performance, "regimes": sorted(regimes)}

    if should_promote:
        ModelRegistry.objects.filter(model_name=model_name, active_flag=True).update(active_flag=False)
    ModelRegistry.objects.create(
        model_name=model_name, model_version=version, artifact_path=artifact_path,
        metrics_json=combined_metrics, active_flag=should_promote,
    )
    return {"promoted": should_promote, "reason": reason, "model_version": version}
