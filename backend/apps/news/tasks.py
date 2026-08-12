"""
Celery tasks for the news/sentiment layer. Scheduled from
config/celery.py's beat_schedule ("poll-news-every-5-minutes").
"""

import logging
from datetime import datetime
from datetime import timezone as dt_timezone

from celery import shared_task
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware, make_aware

logger = logging.getLogger(__name__)


@shared_task
def poll_news_sources():
    """
    For each symbol in settings.WATCHLIST: fetch recent headlines from
    our own RSS-based news system (apps.news.rss_client -- real public
    RSS feeds + Google News search, no paid API/key), score each with
    FinBERT (apps.news.finbert), and save as NewsSentiment rows --
    skipping any headline already saved for that symbol+source+
    published_at (rss_client's own 6-hour lookback overlap means a
    5-minutes-later poll will usually return some of the same articles
    it returned last time).

    NEWSAPI_KEY is no longer required or used here -- apps.news.newsapi_client
    is kept in the codebase unused (a paid-provider fallback, if ever
    wanted) but is not the default source.
    """
    from .models import NewsSentiment
    from . import finbert, impact_engine, rss_client

    results = []
    for symbol in settings.WATCHLIST:
        try:
            saved = _poll_symbol(symbol, rss_client, finbert, impact_engine, NewsSentiment)
            results.append({"symbol": symbol, "fetched_and_scored": saved})
        except Exception:
            logger.exception("News polling failed for %s", symbol)
            results.append({"symbol": symbol, "error": True})

    return results


def _poll_symbol(symbol: str, news_client, finbert, impact_engine, NewsSentiment) -> int:
    headlines = news_client.fetch_headlines(symbol)
    saved_count = 0

    for item in headlines:
        published_at = _parse_published_at(item["published_at"])

        # Dedup on (symbol, source, published_at) -- headline TEXT isn't
        # used for the dedup key because two different outlets often
        # run near-identical wire-service headlines at slightly
        # different timestamps; source+timestamp is a tighter match for
        # "this is the same article we already scored."
        if NewsSentiment.objects.filter(
            symbol=symbol, source=item["source"], published_at=published_at,
        ).exists():
            continue

        scored = finbert.score_headline(item["headline"])
        news = NewsSentiment.objects.create(
            symbol=symbol,
            headline=item["headline"],
            source=item["source"],
            published_at=published_at,
            sentiment_label=scored["label"],
            sentiment_score=scored["score"],
            confidence=scored["confidence"],
        )
        # manual section 10, pipeline steps 3+5+6 (entity extraction +
        # impact scoring) -- this call is what was missing; the models
        # and logic existed but nothing ever invoked them, so
        # NewsImpactAnalysis rows never got created despite the code
        # being written.
        impact_engine.analyze_impact(news)
        saved_count += 1

    return saved_count


def _parse_published_at(raw: str) -> datetime:
    """
    apps.news.rss_client always returns an already-UTC-aware ISO-8601
    string (see that module's _entry_published_at -- feedparser
    normalizes every feed's published time to UTC regardless of the
    source's own stated timezone). Django's parse_datetime correctly
    recognizes that offset and returns an aware datetime in modern
    versions, so the is_aware() branch below is a defensive fallback,
    not the common path -- but if it DOES trigger, the naive datetime
    must be localized as UTC, never as settings.TIME_ZONE (Asia/Kolkata)
    -- make_aware() without an explicit timezone argument uses the
    *local* default timezone, which would silently shift every
    timestamp by 5.5 hours if applied here.
    """
    dt = parse_datetime(raw)
    if dt is None:
        raise ValueError(f"Could not parse published_at timestamp: {raw!r}")
    if not is_aware(dt):
        dt = make_aware(dt, timezone=dt_timezone.utc)
    return dt
