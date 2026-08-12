"""
manual 13.12 "Strategy Evaluation": ranks every StrategyVersion that
has at least one closed trade, using the same real closed-trade data
apps.analytics.services already aggregates for daily performance
metrics (win rate, profit factor) -- computed fresh on each call
rather than cached, since this is read far less often than a trade
closes.
"""

from __future__ import annotations


def rank_strategies() -> list[dict]:
    from apps.execution.models import OpenPosition
    from .models import StrategyVersion

    rows = []
    for version in StrategyVersion.objects.all():
        closed = OpenPosition.objects.filter(
            signal__strategy_version=version, closed_at__isnull=False,
        )
        count = closed.count()
        if count == 0:
            continue

        pnls = [float(p.unrealized_pnl) for p in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        win_rate = len(wins) / count

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        else:
            profit_factor = float("inf") if gross_profit > 0 else 0.0

        rows.append({
            "version_name": version.version_name,
            "active": version.active_flag,
            "trade_count": count,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
        })

    # Win rate first, profit factor as the tiebreaker -- matches the
    # manual's own metric ordering in 13.12 (Win Rate listed first).
    rows.sort(key=lambda r: (r["win_rate"], r["profit_factor"]), reverse=True)
    return rows
