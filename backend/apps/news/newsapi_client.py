"""
Thin wrapper around NewsAPI.org (manual section 4). Same isolation
principle as apps.market_data.broker_client: everything that touches
the actual HTTP API lives here, so apps.news.tasks doesn't know or
care whether headlines come from NewsAPI specifically vs. some other
provider later.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.utils import timezone as django_timezone

logger = logging.getLogger(__name__)

NEWSAPI_URL = "https://newsapi.org/v2/everything"

# NewsAPI doesn't know what "NIFTY" or "BANKNIFTY" mean as tickers --
# these are search-query strings per watchlist symbol. Same static-map
# pattern as broker_client.SYMBOL_TOKENS, and the same caveat applies:
# this is a reasonable starting point, not a rigorously tuned query --
# review what it actually returns before trusting the sentiment scores
# built on top of it.
SYMBOL_NEWS_QUERIES = {
    "NIFTY": '"Nifty 50" OR "NSE India" OR "Indian stock market"',
    "BANKNIFTY": '"Bank Nifty" OR "Nifty Bank" OR "Indian banking sector"',
}


class NewsApiError(Exception):
    pass


def fetch_headlines(symbol: str, since: datetime | None = None, page_size: int = 20) -> list[dict]:
    """
    Returns a list of dicts: {"headline", "source", "published_at", "url"}.
    `since` defaults to 6 hours ago -- matches the 5-minute polling
    schedule with generous overlap (better to re-fetch a headline
    already scored than to miss one because of a timing edge; the
    caller is responsible for not double-scoring, see
    apps/news/tasks.py's dedup-by-headline-and-source check).

    Uses django_timezone.now() (UTC, since USE_TZ=True), not
    settings.TIME_ZONE-localized time -- NewsAPI's `from`/`to` params
    are UTC per their own docs, unlike Angel One's candle endpoint
    (apps/market_data/broker_client.py), which expects IST. Two
    different external APIs with two different timezone conventions;
    picking the wrong one for either is the same class of "silently
    fetches the wrong window, doesn't error" bug either way.
    """
    if symbol not in SYMBOL_NEWS_QUERIES:
        raise ValueError(
            f"No NewsAPI query configured for {symbol} -- add it to "
            f"SYMBOL_NEWS_QUERIES in apps/news/newsapi_client.py."
        )
    if not settings.NEWSAPI_KEY:
        raise NewsApiError("NEWSAPI_KEY is not set in .env -- cannot fetch headlines.")

    since = since or (django_timezone.now() - timedelta(hours=6))

    response = requests.get(
        NEWSAPI_URL,
        params={
            "q": SYMBOL_NEWS_QUERIES[symbol],
            "from": since.strftime("%Y-%m-%dT%H:%M:%S"),
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": page_size,
            "apiKey": settings.NEWSAPI_KEY,
        },
        timeout=10,
    )

    if response.status_code != 200:
        raise NewsApiError(f"NewsAPI returned {response.status_code}: {response.text[:300]}")

    payload = response.json()
    if payload.get("status") != "ok":
        raise NewsApiError(f"NewsAPI error response: {payload.get('message')}")

    headlines = []
    for article in payload.get("articles", []):
        title = article.get("title")
        if not title or title == "[Removed]":
            # NewsAPI's own placeholder for articles it can no longer
            # serve -- skip rather than scoring garbage sentiment on it.
            continue
        headlines.append({
            "headline": title,
            "source": (article.get("source") or {}).get("name", "unknown"),
            "published_at": article.get("publishedAt"),
            "url": article.get("url", ""),
        })
    return headlines
