"""
Signal Explanation Engine: turns apps.options.confirmation's structured
factor breakdown (and, when supplied, apps.options.anomaly_detection
results) into itemized positiveFactors/negativeFactors/riskFlags lists
-- the frontend-renderable version of the "why" apps.signals.models.
TradingSignal.reason already provides as free text prose.
"""

from __future__ import annotations


def build_signal_explanation(confirmation_result: dict, anomalies: list[dict] | None = None) -> dict:
    """
    confirmation_result: apps.options.confirmation.
    evaluate_multi_signal_confirmation's return value.
    anomalies: optional list of apps.options.anomaly_detection results
    (e.g. [detect_volume_anomaly(...), detect_oi_change_anomaly(...)]) --
    only entries with is_anomaly=True are surfaced as risk flags.

    Returns {"positive_factors": [str,...], "negative_factors": [str,...],
    "risk_flags": [str,...]}. Neutral/unavailable factors are
    deliberately omitted from both positive and negative lists -- only
    genuinely confirming or contradicting factors are itemized, matching
    the example format ("1. Trend is bullish. 2. Momentum is
    accelerating...") rather than padding the list with "IV: neutral".
    """
    positive: list[str] = []
    negative: list[str] = []
    risk_flags: list[str] = []

    all_factors = {
        **confirmation_result.get("directional_factors", {}),
        **confirmation_result.get("setup_quality_factors", {}),
    }
    for factor_name, factor in all_factors.items():
        signal = factor.get("signal")
        detail = factor.get("detail", "")
        label = factor_name.replace("_", " ").title()
        if signal in ("bullish", "favorable"):
            positive.append(f"{label}: {detail}" if detail else label)
        elif signal in ("bearish", "unfavorable"):
            negative.append(f"{label}: {detail}" if detail else label)

    conflict_level = confirmation_result.get("conflict_level")
    if conflict_level in ("CONFLICT_HIGH", "CONFLICT_MEDIUM"):
        risk_flags.append(f"{conflict_level.replace('_', ' ').title()}: {confirmation_result.get('conflict_detail', '')}")

    for anomaly in anomalies or []:
        if anomaly.get("is_anomaly"):
            risk_flags.append(f"Anomaly detected: {anomaly.get('detail', '')}")

    return {"positive_factors": positive, "negative_factors": negative, "risk_flags": risk_flags}
