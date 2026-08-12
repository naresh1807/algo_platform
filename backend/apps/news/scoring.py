"""
Aggregates recent NewsSentiment rows for a symbol into the single
sentiment_score + a "no strong contradictory headlines" flag that
apps.signals.engine needs (manual section 11: "News sentiment positive"
/ "No strong contradictory headlines").

Deliberately NOT a standalone trigger -- per the manual's core design
principle ("Sentiment is a filter, not a standalone trigger"), this
module only ever produces a score and a veto flag for
apps.signals.engine to combine with the technical score; nothing here
can generate a BUY/SELL decision by itself.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import NewsSentiment

LOOKBACK_HOURS = 48
CONTRADICTORY_HEADLINE_THRESHOLD = -0.5  # a single very-negative headline counts as "strong"
MIN_CONFIDENCE_TO_COUNT = 0.6  # low-confidence FinBERT scores are excluded, not just down-weighted

# apps.news.impact_engine._estimate_index_impact's own naming
# convention ("NIFTY_UP"/"NIFTY_DOWN"/"BANKNIFTY_UP"/"BANKNIFTY_DOWN")
# -- see _is_relevant_to_symbol's docstring for why this exists.
_INDEX_IMPACT_PREFIXES = {"NIFTY": "NIFTY_", "BANKNIFTY": "BANKNIFTY_"}


def _is_relevant_to_symbol(headline, symbol: str) -> bool:
    """
    For NIFTY/BANKNIFTY specifically: only headlines apps.news.
    impact_engine already classified as macro (NIFTY) or banking-sector
    (BANKNIFTY) market-wide news actually count as relevant to that
    index -- see NewsImpactAnalysis.index_impact, which
    _estimate_index_impact deliberately leaves blank for anything else
    (a single named stock's own news, an unrelated sector, etc.).

    Without this filter, aggregate_sentiment treated EVERY headline
    apps.news.rss_client tags with symbol="NIFTY"/"BANKNIFTY" as
    equally relevant -- including apps.news.rss_client.
    GENERAL_MARKET_FEEDS content that's actually about one unrelated
    company's own earnings (e.g. "PI Industries shares fall 10% after
    weak Q1 results"). In practice this let ordinary single-stock news
    veto the "no strong contradictory headline" buy condition for the
    INDEX ~93% of the time across this platform's real signal history
    -- almost never a genuine index-level bearish catalyst.

    Any symbol other than NIFTY/BANKNIFTY keeps the previous,
    unfiltered behavior: nothing in this codebase currently evaluates
    sentiment for an individual stock, and an equivalent DIRECT/SECTOR-
    based relevance filter for that case deserves its own dedicated
    design (matching the headline's named entities against the actual
    symbol), not a guess bolted on here.

    A headline with no NewsImpactAnalysis row yet (impact scoring
    failed, or a legacy row from before apps.news.tasks started calling
    analyze_impact() for every headline) is treated as NOT confirmed
    relevant -- same "unknown isn't automatically counted against you"
    stance the rest of this module already takes with MIN_CONFIDENCE_TO_COUNT.
    """
    prefix = _INDEX_IMPACT_PREFIXES.get(symbol)
    if prefix is None:
        return True
    analysis = getattr(headline, "impact_analysis", None)
    return analysis is not None and analysis.index_impact.startswith(prefix)


def aggregate_sentiment(symbol: str) -> dict:
    """
    Returns a dict with:
      - sentiment_score: recency-weighted average, -1.0 to +1.0 (0.0 if no news)
      - confidence: average confidence of the headlines used
      - has_contradictory_headline: True if any single recent headline
        is strongly negative with high confidence -- this is what lets
        one bad headline veto an otherwise-good BULLISH technical
        setup, per section 11's "no strong contradictory headlines"
        buy condition
      - has_strongly_positive_headline: mirror image, for a BEARISH
        setup (a strong positive headline is what contradicts "the
        market is moving down") -- apps.signals.engine never needed
        this (bullish-only), apps.options.index_direction_strategy does
      - headline_count: how many headlines fed the score, so a signal
        with zero recent news can be handled differently (neutral,
        not "confirmed positive") than one with several positive articles
    """
    since = timezone.now() - timedelta(hours=LOOKBACK_HOURS)
    headlines = list(
        NewsSentiment.objects.filter(
            symbol=symbol, published_at__gte=since, confidence__gte=MIN_CONFIDENCE_TO_COUNT,
        ).select_related("impact_analysis").order_by("-published_at")
    )
    headlines = [h for h in headlines if _is_relevant_to_symbol(h, symbol)]

    if not headlines:
        return {
            "sentiment_score": 0.0,
            "confidence": 0.0,
            "has_contradictory_headline": False,
            "headline_count": 0,
        }

    # Recency weighting: the most recent headline in the window gets
    # weight 1.0, linearly decaying to ~0.1 for the oldest one in the
    # lookback window -- a headline from an hour ago should matter more
    # than one from 47 hours ago, but neither should be ignored outright.
    weighted_sum = 0.0
    weight_total = 0.0
    for h in headlines:
        age_fraction = (timezone.now() - h.published_at) / timedelta(hours=LOOKBACK_HOURS)
        weight = max(0.1, 1.0 - age_fraction)
        weighted_sum += h.sentiment_score * weight
        weight_total += weight

    sentiment_score = weighted_sum / weight_total if weight_total else 0.0
    avg_confidence = sum(h.confidence for h in headlines) / len(headlines)

    has_contradictory_headline = any(
        h.sentiment_score <= CONTRADICTORY_HEADLINE_THRESHOLD and h.confidence >= MIN_CONFIDENCE_TO_COUNT
        for h in headlines
    )
    # Mirror image, for a BEARISH thesis (apps.options.index_direction_
    # strategy's PE case): a strongly POSITIVE headline is what
    # contradicts "the market is moving down," not a negative one --
    # apps.signals.engine never needed this (it only ever evaluates a
    # bullish case), but a direction-aware caller does.
    has_strongly_positive_headline = any(
        h.sentiment_score >= -CONTRADICTORY_HEADLINE_THRESHOLD and h.confidence >= MIN_CONFIDENCE_TO_COUNT
        for h in headlines
    )

    return {
        "sentiment_score": round(sentiment_score, 4),
        "confidence": round(avg_confidence, 4),
        "has_contradictory_headline": has_contradictory_headline,
        "has_strongly_positive_headline": has_strongly_positive_headline,
        "headline_count": len(headlines),
    }
