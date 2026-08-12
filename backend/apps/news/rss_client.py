"""
Our own news-gathering system: real headlines from public RSS feeds and
Google News' public RSS search, no API key, no paid provider. Replaces
apps.news.newsapi_client as apps.news.tasks.poll_news_sources' headline
source -- same public interface (fetch_headlines(symbol) -> list of
{"headline", "source", "published_at", "url"} dicts), so nothing
downstream (FinBERT scoring, impact_engine, NewsSentiment) needed to
change to use this instead.

Two source types, both configured in apps.news.rss_sources:
  1. GENERAL_MARKET_FEEDS -- real Indian financial-press RSS feeds,
     always relevant (index-level symbols care about all general market
     news), fetched once per poll cycle and reused across symbols.
  2. SYMBOL_SEARCH_QUERIES -- per-symbol Google News RSS searches, split
     into "direct" (the index/sector itself) and "indirect" (macro/
     global drivers) -- this is the "what news affects the market
     directly and indirectly" requirement; apps.news.impact_engine's
     existing entity/sector extraction does the fine-grained
     DIRECT/SECTOR/MACRO/NONE classification once these headlines are
     scored, so this module's only job is casting a wide, real net.

HONESTY NOTE, same spirit as every other external-integration module in
this codebase: RSS feed URLs and structure are controlled by their
publishers and can change without notice. A feed silently returning
zero entries for a while is worth checking by hand at the publisher's
site, not assumed to mean "no news happened."
"""

from __future__ import annotations

import calendar
import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone

import requests
from django.utils import timezone as django_timezone

from .rss_sources import GENERAL_MARKET_FEEDS, GOOGLE_NEWS_RSS_SEARCH_URL, SYMBOL_SEARCH_QUERIES

logger = logging.getLogger(__name__)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}
REQUEST_TIMEOUT = 10

# Short-lived, in-process cache for the general feeds (not the
# per-symbol Google News searches, which already differ per call) --
# apps.news.tasks.poll_news_sources calls fetch_headlines() once per
# WATCHLIST symbol in the same task run, seconds apart; without this,
# every general feed would be fetched once per symbol for no benefit
# (the content is identical). TTL, not a permanent cache, since this is
# a process-lifetime dict, not Redis -- a long-lived Celery worker
# should still see fresh headlines on the next poll cycle.
_GENERAL_FEED_CACHE: dict[str, tuple[float, list[dict]]] = {}
_GENERAL_FEED_CACHE_TTL_SECONDS = 120


class NewsFetchError(Exception):
    pass


def _entry_published_at(entry) -> datetime | None:
    """
    feedparser normalizes published_parsed/updated_parsed to a UTC
    time.struct_time regardless of the feed's own stated timezone (its
    own documented behavior) -- calendar.timegm (NOT time.mktime, which
    assumes local time) is the correct way to turn that back into a
    real UTC timestamp without silently shifting it by the host
    machine's timezone offset.
    """
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(struct), tz=dt_timezone.utc)


def _entry_source_name(entry, fallback: str) -> str:
    # Google News RSS entries carry the ORIGINAL publisher in
    # entry.source.title (e.g. "Reuters", "LiveMint") -- more useful for
    # dedup/review than "Google News" for every single article. Falls
    # back to the feed's own name (general feeds, or if a Google News
    # entry is missing this for some reason).
    source = entry.get("source")
    if isinstance(source, dict) and source.get("title"):
        return source["title"]
    return fallback


def _parse_feed(url: str, feed_label: str) -> list[dict]:
    import feedparser

    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except Exception as exc:
        logger.warning("rss_client: failed to fetch %s (%s): %s", feed_label, url, exc)
        return []

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        logger.warning(
            "rss_client: %s returned unparseable content (bozo_exception=%s) -- skipping.",
            feed_label, getattr(parsed, "bozo_exception", None),
        )
        return []

    items = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        published_at = _entry_published_at(entry)
        if published_at is None:
            continue
        items.append({
            "headline": title,
            "source": _entry_source_name(entry, feed_label),
            "published_at": published_at.isoformat(),
            "url": entry.get("link", ""),
        })
    return items


def _fetch_general_feeds() -> list[dict]:
    now = time.monotonic()
    all_items = []
    for feed_label, url in GENERAL_MARKET_FEEDS:
        cached = _GENERAL_FEED_CACHE.get(url)
        if cached is not None and (now - cached[0]) < _GENERAL_FEED_CACHE_TTL_SECONDS:
            all_items.extend(cached[1])
            continue
        items = _parse_feed(url, feed_label)
        _GENERAL_FEED_CACHE[url] = (now, items)
        all_items.extend(items)
    return all_items


def _fetch_google_news_search(query: str, label: str) -> list[dict]:
    try:
        response = requests.get(
            GOOGLE_NEWS_RSS_SEARCH_URL,
            params={"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
            headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("rss_client: Google News search failed for %s: %s", label, exc)
        return []

    import feedparser
    parsed = feedparser.parse(response.content)
    items = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        published_at = _entry_published_at(entry)
        if published_at is None:
            continue
        items.append({
            "headline": title,
            "source": _entry_source_name(entry, "Google News"),
            "published_at": published_at.isoformat(),
            "url": entry.get("link", ""),
        })
    return items


def fetch_headlines(symbol: str, since: datetime | None = None, page_size: int = 20) -> list[dict]:
    """
    Same signature/return shape as the old newsapi_client.fetch_headlines
    (see that module, kept in the codebase unused as a reference/fallback
    if a paid provider is ever wanted again) -- apps.news.tasks doesn't
    need to change to use this instead.

    page_size caps the return count (most-recent-first) rather than
    limiting what's fetched -- general feeds + two Google News searches
    (direct + indirect) commonly return 100+ combined raw items per
    symbol; page_size keeps FinBERT scoring (apps.news.finbert, a real
    transformer model -- not free computationally) from being run
    against an unbounded pile every 5 minutes.
    """
    if symbol not in SYMBOL_SEARCH_QUERIES:
        raise ValueError(
            f"No RSS search queries configured for {symbol} -- add it to "
            f"SYMBOL_SEARCH_QUERIES in apps/news/rss_sources.py."
        )

    since = since or (django_timezone.now() - timedelta(hours=6))

    queries = SYMBOL_SEARCH_QUERIES[symbol]
    raw_items = (
        _fetch_general_feeds()
        + _fetch_google_news_search(queries["direct"], f"{symbol} (direct)")
        + _fetch_google_news_search(queries["indirect"], f"{symbol} (indirect/macro)")
    )

    # Dedup on (headline, source) within this fetch -- the same real
    # article commonly appears in multiple feeds/searches (e.g. a
    # Reuters wire story picked up by both a general feed and a Google
    # News search), and apps.news.tasks' own DB-level dedup is keyed on
    # (symbol, source, published_at), which wouldn't catch two identical
    # headlines from the same source with microsecond-different
    # published_at parsing between the two fetch paths.
    seen = set()
    deduped = []
    for item in raw_items:
        key = (item["headline"], item["source"])
        if key in seen:
            continue
        seen.add(key)
        if datetime.fromisoformat(item["published_at"]) >= since:
            deduped.append(item)

    deduped.sort(key=lambda i: i["published_at"], reverse=True)
    return deduped[:page_size]
