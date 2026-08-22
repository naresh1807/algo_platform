"""
Valid CE/PE contract selection -- reuses (never duplicates)
apps.options.strike_selector.suggest_best_strike for the actual
strike-picking logic and apps.options.expiry_service for the
not-expired check (suggest_best_strike does NOT self-check expiry --
confirmed by reading it; a caller must resolve/validate the expiry
first, which is exactly what this module does).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import market_data_reader


@dataclass
class ContractCandidate:
    contract: "object"  # apps.options.models.OptionContract
    quote: market_data_reader.ContractQuote
    expiry_days: int


def select_contract(underlying: str, option_type: str) -> tuple[ContractCandidate | None, str]:
    """`option_type`: "CE" or "PE" -- mapped to suggest_best_strike's own
    "bullish"/"bearish" direction vocabulary here so this module's
    callers can think in the same CE/PE terms as the rest of this app's
    action space (Action.BUY_CE_ONE_LOT / BUY_PE_ONE_LOT)."""
    from django.utils import timezone

    from apps.options.expiry_service import is_expiry_eligible, resolve_current_expiry
    from apps.options.models import OptionContract
    from apps.options.strike_selector import suggest_best_strike

    expiry = resolve_current_expiry(underlying)
    if expiry is None:
        return None, f"no valid non-expired expiry available for {underlying}"
    if not is_expiry_eligible(expiry):
        return None, f"resolved expiry {expiry} is not eligible right now (rollover pending)"

    direction = "bullish" if option_type == "CE" else "bearish"
    result = suggest_best_strike(underlying, expiry, direction)
    suggested = result["suggested"]
    if suggested is None:
        return None, result["reason"]

    try:
        contract = OptionContract.objects.get(pk=suggested["contract_id"])
    except OptionContract.DoesNotExist:
        return None, "suggested contract no longer exists in the contract master"

    if not contract.is_active:
        return None, f"contract {contract} is not active (expired or rolled over)"
    if not contract.symbol_token or not contract.lot_size:
        return None, f"contract {contract} has no valid token or lot size"

    quote = market_data_reader.latest_contract_quote(contract)
    if quote is None or quote.ltp is None:
        return None, f"no live quote available for {contract}"

    expiry_days = (expiry - timezone.localdate()).days
    return ContractCandidate(contract=contract, quote=quote, expiry_days=expiry_days), result["reason"]
