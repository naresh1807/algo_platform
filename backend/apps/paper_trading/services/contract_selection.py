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


def select_contract(underlying: str, option_type: str, account=None) -> tuple[ContractCandidate | None, str]:
    """`option_type`: "CE" or "PE" -- mapped to suggest_best_strike's own
    "bullish"/"bearish" direction vocabulary here so this module's
    callers can think in the same CE/PE terms as the rest of this app's
    action space (Action.BUY_CE_ONE_LOT / BUY_PE_ONE_LOT)."""
    from django.utils import timezone

    from apps.options.expiry_service import is_expiry_eligible, resolve_current_expiry
    from apps.options.models import OptionContract, get_options_strategy_settings
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

    # Keep the preferred strike first, then only ranked delta-qualified
    # alternatives. Every alternative must pass the same hard entry gates.
    choices = [suggested]
    if account is not None and get_options_strategy_settings().strike_mode.lower() == "ai":
        choices += sorted([c for c in result.get("candidates", [])
                           if c.get("in_sweet_spot") and c["contract_id"] != suggested["contract_id"]],
                          key=lambda c: c.get("score", 0), reverse=True)[:9]
    last_reason = "no eligible contract"
    for choice in choices:
        try:
            contract = OptionContract.objects.get(pk=choice["contract_id"])
        except OptionContract.DoesNotExist:
            last_reason = "suggested contract no longer exists"; continue
        if not contract.is_active or not contract.symbol_token or not contract.lot_size:
            last_reason = "inactive contract or invalid token/lot size"; continue
        quote = market_data_reader.latest_contract_quote(contract)
        if quote is None or quote.ltp is None:
            last_reason = "no live quote available"; continue
        if account is not None:
            from .paper_risk_engine import check_pre_entry
            check = check_pre_entry(account, contract, ltp=quote.ltp, bid=quote.bid, ask=quote.ask,
                open_interest=quote.open_interest, volume=quote.volume, feed_age_seconds=quote.age_seconds)
            if not check.approved:
                last_reason = check.reason_text; continue
        detail = result["reason"] if choice is suggested else "Alternative delta-qualified strike passed cash, freshness and liquidity checks"
        return ContractCandidate(contract=contract, quote=quote,
            expiry_days=(expiry-timezone.localdate()).days), detail
    return None, last_reason
