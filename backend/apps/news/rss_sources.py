"""
Static config for apps.news.rss_client -- same "small static map, review
before trusting" pattern as apps.market_data.broker_client.SYMBOL_TOKENS
and the newsapi_client.py this module replaces.

Two kinds of sources, both free, no API key, no account:

1. GENERAL_MARKET_FEEDS: real, publicly-syndicated RSS feeds from Indian
   financial news outlets. Broad Indian-market coverage -- relevant to
   every symbol this platform trades (NIFTY/BANKNIFTY are indices, not
   single companies, so general market/economy news is always in scope).
   Verified reachable (HTTP 200, real parseable RSS/XML) on 2026-08-08 --
   outlets do occasionally restructure their RSS URLs, so a feed
   returning zero entries for a while is worth rechecking by hand at the
   publisher's own site (usually linked from their footer as "RSS").

2. SYMBOL_SEARCH_QUERIES: per-symbol search queries run against Google
   News' public RSS search endpoint (news.google.com/rss/search) --
   no API key, returns real headlines from hundreds of outlets (Reuters,
   Bloomberg, Indian financial press, etc.) with per-article source
   attribution. Each symbol's query is split into DIRECT terms (the
   index/sector itself) and INDIRECT/macro terms (global events that
   move Indian markets without naming them specifically -- Fed policy,
   crude oil, US markets, dollar/rupee, geopolitical shocks) --
   apps.news.impact_engine's existing DIRECT/SECTOR/MACRO/NONE
   trade_relevance classification is exactly what separates these back
   out downstream, so this module doesn't need to tag them itself.
"""

from __future__ import annotations

GENERAL_MARKET_FEEDS = [
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol Business", "https://www.moneycontrol.com/rss/business.xml"),
    ("Moneycontrol Market Reports", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Moneycontrol Economy", "https://www.moneycontrol.com/rss/economy.xml"),
    ("LiveMint Markets", "https://www.livemint.com/rss/markets"),
    ("Business Standard Markets", "https://www.business-standard.com/rss/markets-106.rss"),
    ("CNBC-TV18 Market", "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"),
]

# DIRECT: the index/sector by name. INDIRECT: macro/global drivers that
# move Indian index prices without ever mentioning "Nifty" or "Bank
# Nifty" -- these are what "indirectly affects the market" means in
# practice (US Fed policy -> FII flows -> Nifty; crude oil -> import
# bill/inflation -> RBI policy -> Bank Nifty; etc.).
SYMBOL_SEARCH_QUERIES = {
    "NIFTY": {
        "direct": '"Nifty 50" OR Sensex OR "NSE India" OR "Indian stock market" OR FII OR FPI',
        "indirect": (
            '"Federal Reserve" OR "Fed rate" OR "crude oil price" OR "US markets" '
            'OR "dollar index" OR "rupee" OR "US inflation" OR "China economy" '
            'OR "global markets"'
        ),
    },
    "BANKNIFTY": {
        "direct": '"Bank Nifty" OR "Nifty Bank" OR "Indian banking sector" OR RBI OR "bank stocks"',
        "indirect": (
            '"RBI repo rate" OR "interest rate" OR "Federal Reserve" OR "bond yields" '
            'OR "credit growth" OR "NPA banking" OR "liquidity crisis"'
        ),
    },
}

GOOGLE_NEWS_RSS_SEARCH_URL = "https://news.google.com/rss/search"
