"""
ModelEvaluationService -- the R-multiple-based performance metrics
(profit factor, expectancy, max drawdown, regime coverage) the
promotion gates need beyond the raw accuracy/Brier score
model_training_service already reports. Operates on the SAME
chronological holdout rows training used (never re-derives its own
split), so these numbers describe exactly the out-of-sample period the
reported accuracy came from.
"""

from __future__ import annotations


def evaluate_holdout_performance(holdout_rows) -> dict:
    net_rs = [float(row.net_r) for row in holdout_rows if row.net_r is not None]
    if not net_rs:
        return {"sample_count": 0, "profit_factor": None, "expectancy_r": None, "max_drawdown_r": None}

    wins = [r for r in net_rs if r > 0]
    losses = [r for r in net_rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    expectancy_r = sum(net_rs) / len(net_rs)

    running = 0.0
    peak = float("-inf")
    max_dd = 0.0
    for value in net_rs:
        running += value
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    return {
        "sample_count": len(net_rs), "win_count": len(wins), "loss_count": len(losses),
        "profit_factor": profit_factor, "expectancy_r": expectancy_r, "max_drawdown_r": max_dd,
    }


def regimes_represented(holdout_rows) -> set[str]:
    regimes: set[str] = set()
    for row in holdout_rows:
        regime = (row.feature_vector_json or {}).get("regime")
        if regime:
            regimes.add(regime)
    return regimes
