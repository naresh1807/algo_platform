"""
Angel One option-chain access (manual section 9). Kept as a SEPARATE
module from apps.market_data.broker_client rather than added to it --
option-chain endpoints (instrument search, option Greeks/quote-with-OI)
are a meaningfully different part of Angel One's API surface than the
candle-history endpoint that module wraps, and this app (apps.options)
owns its own broker access the same way apps.market_data owns candle
ingestion -- no cross-app reach-in.

SAME CAVEAT AS market_data/broker_client.py, narrower now: contract
discovery (fetch_contract_list) is wired to Angel One's real, public
instrument master (apps/options/instrument_master.py) with a format
confirmed against Angel One's own SmartAPI forum/docs. fetch_chain_quotes
still carries the original caveat -- its exact getMarketData() response
field names (opnInterest/openInterest, bestBidPrice, etc.) are a
best-effort guess informed by common SmartAPI usage, not independently
verified against a live response, and IV is left as None since Angel
One's standard quote payload doesn't reliably include it (their
separate OptionGreek endpoint would be needed for that).
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime

from apps.market_data.broker_client import BrokerClient, BrokerAuthError

logger = logging.getLogger(__name__)

# Angel One's documented per-request cap for getMarketData -- see
# fetch_chain_quotes below for what happens if this is exceeded.
MARKET_DATA_BATCH_SIZE = 50


class OptionChainClient(BrokerClient):
    """
    Extends BrokerClient (reusing its lazy _connect()/session handling)
    rather than duplicating login logic -- one Angel One session is
    shared for both candle and option-chain access if both are used in
    the same process.
    """

    def fetch_contract_list(self, underlying: str, expiry: date) -> list[dict]:
        """
        Returns [{"strike", "option_type", "symbol_token"}, ...] for
        every listed contract on this underlying+expiry, sourced from
        Angel One's public instrument master (apps/options/instrument_master.py)
        rather than a dedicated "get option chain" REST call -- Angel
        One doesn't expose one directly, as of the SmartAPI version
        this was written against. The instrument master download is
        cached (24h TTL) so calling this repeatedly (e.g. once per
        underlying in sync_option_contracts) doesn't re-download the
        full multi-MB file each time.
        """
        from .instrument_master import options_for_expiry

        contracts = options_for_expiry(underlying, expiry)
        if not contracts:
            logger.warning(
                "No option contracts found for %s expiry %s in the instrument master -- "
                "double check the expiry actually exists (see instrument_master.list_expiries).",
                underlying, expiry,
            )
        return contracts

    def fetch_chain_quotes(self, contracts: list[dict]) -> list[dict]:
        """
        contracts: [{"symbol_token", "strike", "option_type"}, ...]
        (typically from OptionContract rows already in the DB, not
        fetch_contract_list's raw output).

        Returns one dict per contract: {"symbol_token", "ltp",
        "open_interest", "volume", "iv", "bid", "ask"}.

        Uses Angel One's quote API in batches (SmartAPI's getMarketData
        supports fetching multiple tokens in one call, up to a
        provider-specific limit) rather than one call per contract --
        an index expiry can have 200+ contracts, and 200+ individual
        HTTP round-trips every polling cycle would be both slow and a
        likely target for the broker's own rate limiting.

        MARKET_DATA_BATCH_SIZE=50 is Angel One's documented per-request
        cap for getMarketData -- sending everything in one call (as an
        earlier version of this function did) doesn't return a clean
        API error, it gets flat-out rejected by Angel One's edge/WAF
        ("Request Rejected... consult your administrator") before ever
        reaching application logic, which is a much more confusing
        failure to debug than a normal API error would be.
        """
        connection = self._connect()
        tokens = [c["symbol_token"] for c in contracts]

        quotes_by_token: dict[str, dict] = {}
        for i in range(0, len(tokens), MARKET_DATA_BATCH_SIZE):
            batch = tokens[i:i + MARKET_DATA_BATCH_SIZE]
            response = connection.getMarketData(
                mode="FULL", exchangeTokens={"NFO": batch},
            )
            if not response.get("status"):
                raise RuntimeError(f"Option chain quote fetch failed: {response.get('message')}")
            for q in response.get("data", {}).get("fetched", []):
                quotes_by_token[str(q.get("symbolToken"))] = q

        results = []
        for contract in contracts:
            q = quotes_by_token.get(contract["symbol_token"])
            if q is None:
                logger.warning("No quote returned for token %s", contract["symbol_token"])
                continue
            results.append({
                "symbol_token": contract["symbol_token"],
                "ltp": q.get("ltp"),
                "open_interest": q.get("opnInterest") or q.get("openInterest") or 0,
                "volume": q.get("tradeVolume") or 0,
                # NOTE: Angel One's standard quote payload does not
                # reliably include implied volatility -- IV typically
                # needs their separate OptionGreek endpoint. Left as
                # None here rather than guessing a wrong field name;
                # wire in a real optionGreek() call if IV-dependent
                # signals (IV rank, IV crush warning) need to be trusted.
                "iv": None,
                "bid": q.get("bestBidPrice"),
                "ask": q.get("bestAskPrice"),
            })
        return results


_shared_client_lock = threading.Lock()
_shared_client: "OptionChainClient | None" = None


def get_option_chain_client() -> "OptionChainClient":
    """
    Returns ONE OptionChainClient shared for the lifetime of this
    process -- same reasoning, same bug, as
    apps.market_data.broker_client.get_broker_client(): this class's
    own docstring already said "one Angel One session is shared... if
    both are used in the same process," but
    apps.options.tasks.sync_watchlist_option_contracts/
    ingest_option_chain_snapshots were each doing
    `client = OptionChainClient()` fresh on every task run instead,
    meaning a fresh, unthrottled Angel One login on every invocation
    (weekly for the former, but ingest_option_chain_snapshots runs far
    more often -- see config/celery.py). Kept as its own singleton
    here rather than folding into get_broker_client(): apps.options
    deliberately owns its own broker access (see this module's own
    docstring on why it's a separate module at all), so this is a
    second Angel One session for the process, not a shared one with
    apps.market_data/apps.investing/apps.execution's BrokerClient --
    still a large improvement over a fresh login per task run.
    """
    global _shared_client
    with _shared_client_lock:
        if _shared_client is None:
            _shared_client = OptionChainClient()
        return _shared_client
