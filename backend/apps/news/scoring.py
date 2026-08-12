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


def aggregate_sentiment(symbol: str) -> dict:
    """
    Returns a dict with:
      - sentiment_score: recency-weighted average, -1.0 to +1.0 (0.0 if no news)
      - confidence: average confidence of the headlines used
      - has_contradictory_headline: True if any single recent headline
        is strongly negative with high confidence -- this is what lets
        one bad headline veto an otherwise-good technical setup, per
        section 11's "no strong contradictory headlines" buy condition
      - headline_count: how many headlines fed the score, so a signal
        with zero recent news can be handled differently (neutral,
        not "confirmed positive") than one with several positive articles
    """
    since = timezone.now() - timedelta(hours=LOOKBACK_HOURS)
    headlines = list(
        NewsSentiment.objects.filter(
            symbol=symbol, published_at__gte=since, confidence__gte=MIN_CONFIDENCE_TO_COUNT,
        ).order_by("-published_at")
    )

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

    return {
        "sentiment_score": round(sentiment_score, 4),
        "confidence": round(avg_confidence, 4),
        "has_contradictory_headline": has_contradictory_headline,
        "headline_count": len(headlines),
    }
