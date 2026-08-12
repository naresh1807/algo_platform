"""
manual section 10, pipeline steps 5-6: "Score market impact" / "Rank
likely stock and sector effects". Combines apps.news.entities'
extraction with a NewsSentiment row's existing FinBERT score into a
NewsImpactAnalysis row.

IMPORTANT HONESTY NOTE: "likely_rising_stocks" / "likely_falling_stocks"
here means exactly one thing -- a stock was DIRECTLY NAMED in a
headline that FinBERT scored as positive/negative. This is NOT a
predictive model forecasting which stocks will move; it's a
transparent, mechanical rule (named + positive => "rising" bucket,
named + negative => "falling" bucket) chosen specifically because it's
fully explainable (manual Core Principle: "AI must explain every
signal") and makes no claim beyond what the headline and its sentiment
score directly support. A real stock-impact PREDICTION model (one that
reasons about e.g. an unrelated company being affected by a supplier's
bad earnings) is a substantially different, much harder ML problem
that is not attempted here.
"""

from __future__ import annotations

from .entities import classify_time_horizon, extract_entities
from .models import NewsImpactAnalysis, NewsSentiment

SENTIMENT_RELEVANCE_THRESHOLD = 0.3  # |sentiment_score| below this is treated as "no clear direction"


def analyze_impact(news: NewsSentiment) -> NewsImpactAnalysis:
    """
    Idempotent: safe to call more than once for the same NewsSentiment
    row (e.g. if impact scoring is re-run after entities.py's keyword
    maps are extended) -- update_or_create rather than create, so
    re-analysis replaces the old result instead of erroring on the
    OneToOne constraint or leaving a stale duplicate.
    """
    entities = extract_entities(news.headline)
    time_horizon = classify_time_horizon(news.headline)

    likely_rising, likely_falling = _bucket_stocks_by_sentiment(entities["stocks"], news.sentiment_score)

    trade_relevance = _classify_trade_relevance(entities)
    index_impact = _estimate_index_impact(entities, news.sentiment_score)

    analysis, _created = NewsImpactAnalysis.objects.update_or_create(
        news=news,
        defaults={
            "sectors": entities["sectors"],
            "likely_rising_stocks": likely_rising,
            "likely_falling_stocks": likely_falling,
            "index_impact": index_impact,
            "confidence": news.confidence,
            "time_horizon": time_horizon,
            "trade_relevance": trade_relevance,
        },
    )
    return analysis


def _bucket_stocks_by_sentiment(stocks: list[str], sentiment_score: float) -> tuple[list[str], list[str]]:
    if abs(sentiment_score) < SENTIMENT_RELEVANCE_THRESHOLD or not stocks:
        return [], []
    if sentiment_score > 0:
        return list(stocks), []
    return [], list(stocks)


def _classify_trade_relevance(entities: dict) -> str:
    if entities["stocks"]:
        return NewsImpactAnalysis.TradeRelevance.DIRECT
    if entities["sectors"]:
        if "macro" in entities["sectors"]:
            return NewsImpactAnalysis.TradeRelevance.MACRO
        return NewsImpactAnalysis.TradeRelevance.SECTOR
    return NewsImpactAnalysis.TradeRelevance.NONE


def _estimate_index_impact(entities: dict, sentiment_score: float) -> str:
    """
    Only estimates a NIFTY/BANKNIFTY directional read for macro or
    banking-sector news specifically (the two things this platform's
    watchlist actually trades) -- deliberately blank for anything else
    rather than stretching a single-company headline into an index-wide
    claim it doesn't support.
    """
    if abs(sentiment_score) < SENTIMENT_RELEVANCE_THRESHOLD:
        return ""
    if "macro" in entities["sectors"]:
        return "NIFTY_UP" if sentiment_score > 0 else "NIFTY_DOWN"
    if "banking" in entities["sectors"]:
        return "BANKNIFTY_UP" if sentiment_score > 0 else "BANKNIFTY_DOWN"
    return ""


def generate_market_summary(for_date) -> str:
    """
    manual section 10, pipeline step 7: "Generate Indian market
    summary." A plain-text rollup of the day's macro/sector-relevant
    impact analyses -- intentionally NOT another LLM call (that would
    make this function's output non-deterministic and harder to trust
    for something meant to summarize what the rule-based pipeline
    above actually found); it's a templated summary of the SAME
    structured data already computed, nothing invented beyond it.
    """
    analyses = NewsImpactAnalysis.objects.filter(
        news__published_at__date=for_date,
    ).exclude(trade_relevance=NewsImpactAnalysis.TradeRelevance.NONE).select_related("news")

    if not analyses:
        return f"No market-relevant news identified for {for_date}."

    macro_count = analyses.filter(trade_relevance=NewsImpactAnalysis.TradeRelevance.MACRO).count()
    sector_count = analyses.filter(trade_relevance=NewsImpactAnalysis.TradeRelevance.SECTOR).count()
    direct_count = analyses.filter(trade_relevance=NewsImpactAnalysis.TradeRelevance.DIRECT).count()

    rising = sorted({s for a in analyses for s in a.likely_rising_stocks})
    falling = sorted({s for a in analyses for s in a.likely_falling_stocks})

    parts = [
        f"{for_date}: {len(analyses)} market-relevant headline(s) "
        f"({macro_count} macro, {sector_count} sector-wide, {direct_count} stock-specific).",
    ]
    if rising:
        parts.append(f"Positively mentioned: {', '.join(rising)}.")
    if falling:
        parts.append(f"Negatively mentioned: {', '.join(falling)}.")

    return " ".join(parts)
