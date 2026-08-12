"""
manual section 10, pipeline step 3: "Extract entities and sectors."

Deliberately keyword/alias-based, NOT a trained NER model. A real
named-entity-recognition model for Indian company names would need
Indian-market-specific training data (general-purpose NER models
routinely miss or mis-tag Indian company names, ticker slang, and
Hindi-English code-switched financial media text) that doesn't exist
off-the-shelf here -- building and validating that properly is a
separate, substantial piece of work, not something to fake with a
generic NER library and call it done. This keyword approach is
transparent about its own limits: it will only catch companies/sectors
whose names or common aliases are in the maps below, and nothing else.
Extend the maps as gaps are found in practice, rather than treating
this as a finished, comprehensive system.
"""

from __future__ import annotations

import re

# NIFTY50-ish large caps + common aliases/short forms seen in Indian
# financial media. NOT exhaustive -- a real deployment should keep
# growing this list from headlines it fails to match, not treat it as
# static.
STOCK_ALIASES: dict[str, list[str]] = {
    "RELIANCE": ["reliance industries", "reliance", "ril"],
    "TCS": ["tata consultancy services", "tcs"],
    "INFY": ["infosys"],
    "HDFCBANK": ["hdfc bank", "hdfc"],
    "ICICIBANK": ["icici bank", "icici"],
    "SBIN": ["state bank of india", "sbi"],
    "BHARTIARTL": ["bharti airtel", "airtel"],
    "ITC": ["itc limited", "itc"],
    "LT": ["larsen & toubro", "larsen and toubro", "l&t"],
    "HINDUNILVR": ["hindustan unilever", "hul"],
    "AXISBANK": ["axis bank"],
    "KOTAKBANK": ["kotak mahindra bank", "kotak bank"],
    "MARUTI": ["maruti suzuki", "maruti"],
    "SUNPHARMA": ["sun pharma", "sun pharmaceutical"],
    "TATAMOTORS": ["tata motors"],
    "ADANIENT": ["adani enterprises"],
    "WIPRO": ["wipro"],
    "HCLTECH": ["hcl technologies", "hcl tech"],
}

SECTOR_KEYWORDS: dict[str, list[str]] = {
    "banking": ["bank", "banking", "rbi", "repo rate", "nbfc", "lending"],
    "it": ["it sector", "software services", "tech stocks", "it stocks"],
    "auto": ["auto sector", "automobile", "vehicle sales", "ev maker"],
    "pharma": ["pharma", "pharmaceutical", "drug approval", "usfda"],
    "fmcg": ["fmcg", "consumer goods", "fast moving consumer"],
    "energy": ["oil and gas", "crude oil", "energy sector", "opec"],
    "metals": ["metal stocks", "steel", "iron ore", "aluminium", "copper"],
    "realty": ["real estate", "realty stocks", "housing sector"],
    "macro": [
        "rbi policy", "inflation", "gdp growth", "fiscal deficit",
        "interest rate", "monetary policy", "budget", "fii flows", "dii flows",
        # Extended 2026-08-08 after apps.news.rss_client (our own
        # RSS/Google-News-based headline source, replacing NewsAPI)
        # surfaced real headlines this list was missing entirely --
        # Fed/dollar/rupee/gold/FII-without-"flows"/Wall Street/bond
        # yields are all genuinely macro, index-wide drivers (the
        # "indirectly affects the market" case: none of these name
        # Nifty or Bank Nifty, but all of them move FII positioning and
        # therefore index prices). Per this module's own docstring:
        # extend as gaps are found in practice, not treated as finished.
        "federal reserve", "fed rate", "fed hike", "fed policy", "fed chair",
        "dollar index", "us dollar", "rupee", "gold price", "gold prices",
        "crude oil price", "fii", "fiis", "fii inflow", "fii outflow",
        "fpi", "fpis", "wall street", "us markets", "us stocks",
        "us jobs data", "us inflation", "bond yield", "bond yields",
        "global markets", "china economy", "geopolitical", "opec",
    ],
}

# Words/phrases signaling how long an event's effect is likely to
# matter -- crude but explainable (manual: "AI must explain every
# signal"), rather than a black-box time-horizon classifier.
_INTRADAY_MARKERS = ["today", "this morning", "opening bell", "pre-market", "flash"]
_MULTI_DAY_MARKERS = ["quarter", "quarterly results", "q1", "q2", "q3", "q4", "annual", "guidance", "outlook"]


def extract_entities(headline: str) -> dict:
    """
    Returns {"stocks": [ticker, ...], "sectors": [sector_name, ...]} --
    both lists deduplicated, case-insensitive substring matching against
    the alias maps above. A headline can legitimately match multiple
    stocks and/or sectors (e.g. "Bank Nifty falls as HDFC Bank, ICICI
    Bank drag index lower" matches both a sector and two stocks).
    """
    text = headline.lower()

    stocks = [
        ticker for ticker, aliases in STOCK_ALIASES.items()
        if any(_word_boundary_match(alias, text) for alias in aliases)
    ]
    sectors = [
        sector for sector, keywords in SECTOR_KEYWORDS.items()
        if any(_word_boundary_match(kw, text) for kw in keywords)
    ]
    return {"stocks": stocks, "sectors": sectors}


def _word_boundary_match(phrase: str, text: str) -> bool:
    """
    Plain substring matching (`phrase in text`) would false-positive on
    partial words -- e.g. "itc" as a substring would match inside
    "capacity" -- re.search with \\b word boundaries avoids that class
    of mistake for the short aliases (itc, l&t, hul) that are most at
    risk of it.
    """
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def classify_time_horizon(headline: str) -> str:
    """
    Returns "intraday", "multi_day", or "unspecified". Deliberately a
    simple keyword check, matching the same "transparent heuristic over
    opaque model" choice as the rest of this module.
    """
    text = headline.lower()
    if any(marker in text for marker in _INTRADAY_MARKERS):
        return "intraday"
    if any(marker in text for marker in _MULTI_DAY_MARKERS):
        return "multi_day"
    return "unspecified"
