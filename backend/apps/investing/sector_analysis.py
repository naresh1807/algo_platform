"""
Sector-wise view over apps.investing.fundamental_score's own output --
the trader asked explicitly for a sector-wise breakdown ("సెక్టార్
వైస్‌గా వెరిఫై చేయగలగాలి, అక్కడ వచ్చిన ప్రాఫిటబుల్ స్టాక్‌ని సజెస్ట్
చేయాలి" -- verify sector-wise, suggest the profitable stock there).

This does NOT run any new scoring -- it groups the LATEST
StockRecommendation per stock (apps.investing.tasks.
generate_stock_recommendations' own weekly output) by Stock.sector,
same "read the system's actual last output, don't recompute live"
principle apps.jarvis.responses.best_strategy uses for
apps.learning.ranking. A sector average is only as good as how many
stocks in that sector this app has actually scored -- a sector with
one tracked stock isn't a real "sector average," it's one stock's
score; see MIN_STOCKS_FOR_SECTOR_AVERAGE below.
"""

from __future__ import annotations

MIN_STOCKS_FOR_SECTOR_AVERAGE = 2


def _latest_recommendations():
    """
    Same MySQL-safe "latest row per stock" pattern used throughout this
    app (`.distinct("stock_id")` is Postgres-only DISTINCT ON, not
    available on this project's MySQL system of record). Kept here
    rather than in views.py so both the REST layer
    (StockRecommendationViewSet.get_queryset) and this module's own
    sector_breakdown() share one implementation instead of two that
    could quietly drift apart.
    """
    from django.db.models import Max, Q

    from .models import StockRecommendation

    latest_per_stock = StockRecommendation.objects.values("stock_id").annotate(latest=Max("generated_at"))
    if not latest_per_stock:
        return StockRecommendation.objects.none()

    match_any = Q()
    for row in latest_per_stock:
        match_any |= Q(stock_id=row["stock_id"], generated_at=row["latest"])
    return StockRecommendation.objects.filter(match_any).select_related("stock")


def sector_breakdown() -> list[dict]:
    """
    Returns one dict per sector, sorted best-average-score first:
    {"sector", "avg_score", "stock_count", "strong_hold_count",
    "best_stock": {"symbol", "name", "score", "verdict",
    "suggested_hold_months"} | None}

    `best_stock` is None when the sector has fewer than
    MIN_STOCKS_FOR_SECTOR_AVERAGE scored stocks -- there's a stock,
    just not (yet) a meaningful sector comparison to have picked it
    from.
    """
    sectors: dict[str, list] = {}
    for rec in _latest_recommendations():
        sector = rec.stock.sector or "Unclassified"
        sectors.setdefault(sector, []).append(rec)

    results = []
    for sector, recs in sectors.items():
        scores = [r.fundamental_score for r in recs]
        avg_score = sum(scores) / len(scores)
        strong_hold_count = sum(1 for r in recs if r.verdict == "strong_hold")

        best_stock = None
        if len(recs) >= MIN_STOCKS_FOR_SECTOR_AVERAGE:
            best = max(recs, key=lambda r: r.fundamental_score)
            best_stock = {
                "symbol": best.stock.symbol, "name": best.stock.name,
                "score": best.fundamental_score, "verdict": best.verdict,
                "suggested_hold_months": best.suggested_hold_months,
            }

        results.append({
            "sector": sector,
            "avg_score": round(avg_score, 1),
            "stock_count": len(recs),
            "strong_hold_count": strong_hold_count,
            "best_stock": best_stock,
        })

    results.sort(key=lambda r: r["avg_score"], reverse=True)
    return results
