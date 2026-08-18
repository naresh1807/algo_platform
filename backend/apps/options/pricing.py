"""
Live premium lookup for a single, already-known OptionContract -- used
wherever a decision needs THIS MOMENT's price for one specific contract
(closing a real option position on stop/target), as opposed to
apps.options.tasks.ingest_option_chain_snapshots' periodic sweep (every
300s, see config/celery.py) across every listed contract, which can be
stale by the time an exit check runs.

Deliberately a single-contract call through
apps.options.broker_client.OptionChainClient.fetch_chain_quotes (which
already batches/handles Angel One's getMarketData) rather than a new
broker method -- one contract is just a batch of size 1.
"""

from __future__ import annotations

import logging

from .models import OptionContract

logger = logging.getLogger(__name__)


def latest_ltp_for_contract(contract: OptionContract) -> float | None:
    """
    Returns the contract's current LTP from a live broker quote, or None
    if the fetch failed -- callers must treat None as "can't check this
    position's stop/target right now," not as a zero price.
    """
    from .broker_client import get_option_chain_client

    client = get_option_chain_client()
    try:
        quotes = client.fetch_chain_quotes([{
            "symbol_token": contract.symbol_token,
            "strike": contract.strike,
            "option_type": contract.option_type,
        }])
    except Exception:
        logger.exception("latest_ltp_for_contract: quote fetch failed for %s", contract)
        return None

    if not quotes or quotes[0].get("ltp") is None:
        return None
    return float(quotes[0]["ltp"])
