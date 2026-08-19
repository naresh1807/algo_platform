"""Deterministic transaction-cost accounting for simulated fills.

The paper executor has candle/reference prices but no historical order book,
so it must not invent a spread after seeing whether a trade won.  Instead it
snapshots a preconfigured per-side slippage and fee rate when the position is
opened, applies every fill adversely, and reuses that exact snapshot at exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from common.constants import PositionSide

PRICE_QUANTUM = Decimal("0.0001")
MONEY_QUANTUM = Decimal("0.01")
TEN_THOUSAND = Decimal("10000")


def _decimal(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal number.") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite.")
    return result


@dataclass(frozen=True)
class PaperCostAssumptions:
    slippage_bps_per_side: Decimal
    fees_bps_per_side: Decimal

    def __post_init__(self):
        slippage = _decimal(self.slippage_bps_per_side, "slippage_bps_per_side")
        fees = _decimal(self.fees_bps_per_side, "fees_bps_per_side")
        for label, value in (
            ("slippage_bps_per_side", slippage),
            ("fees_bps_per_side", fees),
        ):
            if value < 0 or value >= TEN_THOUSAND:
                raise ValueError(f"{label} must satisfy 0 <= value < 10000.")
        object.__setattr__(self, "slippage_bps_per_side", slippage)
        object.__setattr__(self, "fees_bps_per_side", fees)


@dataclass(frozen=True)
class PaperSettlement:
    entry_fill_price: Decimal
    exit_fill_price: Decimal
    gross_pnl: Decimal
    slippage_cost: Decimal
    fees: Decimal
    total_costs: Decimal
    net_pnl: Decimal


def configured_assumptions(*, is_option: bool) -> PaperCostAssumptions:
    """Load the cash/option-specific settings selected at position open."""
    from django.conf import settings

    prefix = "PAPER_OPTION" if is_option else "PAPER_CASH"
    return PaperCostAssumptions(
        slippage_bps_per_side=getattr(settings, f"{prefix}_SLIPPAGE_BPS_PER_SIDE"),
        fees_bps_per_side=getattr(settings, f"{prefix}_FEES_BPS_PER_SIDE"),
    )


def assumptions_from_position(position) -> PaperCostAssumptions:
    """Use the immutable snapshot stored on a paper position, never current settings."""
    return PaperCostAssumptions(
        slippage_bps_per_side=(
            position.paper_slippage_bps_per_side
            if position.paper_slippage_bps_per_side is not None else Decimal("0")
        ),
        fees_bps_per_side=(
            position.paper_fees_bps_per_side
            if position.paper_fees_bps_per_side is not None else Decimal("0")
        ),
    )


def adverse_fill_price(
    reference_price: Decimal,
    *,
    side: str,
    is_entry: bool,
    assumptions: PaperCostAssumptions,
) -> Decimal:
    """Return a four-decimal simulated fill rounded against the trader."""
    reference = _decimal(reference_price, "reference_price")
    if reference <= 0:
        raise ValueError("reference_price must be positive.")
    if side not in {PositionSide.LONG, PositionSide.SHORT}:
        raise ValueError(f"Unsupported position side: {side!r}.")

    # A long entry and short exit are buys (slip up); a short entry and
    # long exit are sells (slip down).
    is_buy = (side == PositionSide.LONG and is_entry) or (
        side == PositionSide.SHORT and not is_entry
    )
    rate = assumptions.slippage_bps_per_side / TEN_THOUSAND
    multiplier = Decimal("1") + rate if is_buy else Decimal("1") - rate
    rounding = ROUND_CEILING if is_buy else ROUND_FLOOR
    return (reference * multiplier).quantize(PRICE_QUANTUM, rounding=rounding)


def calculate_paper_settlement(
    *,
    entry_reference_price: Decimal,
    exit_reference_price: Decimal,
    qty: int,
    side: str,
    assumptions: PaperCostAssumptions,
    entry_fill_price: Decimal | None = None,
) -> PaperSettlement:
    """Calculate a reproducible round-trip settlement in currency units."""
    entry_reference = _decimal(entry_reference_price, "entry_reference_price")
    exit_reference = _decimal(exit_reference_price, "exit_reference_price")
    if entry_reference <= 0 or exit_reference <= 0:
        raise ValueError("entry_reference_price and exit_reference_price must be positive.")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise ValueError("qty must be a positive integer.")

    calculated_entry_fill = adverse_fill_price(
        entry_reference, side=side, is_entry=True, assumptions=assumptions,
    )
    if entry_fill_price is None:
        entry_fill = calculated_entry_fill
    else:
        entry_fill = _decimal(entry_fill_price, "entry_fill_price").quantize(PRICE_QUANTUM)
        if entry_fill <= 0:
            raise ValueError("entry_fill_price must be positive.")
    exit_fill = adverse_fill_price(
        exit_reference, side=side, is_entry=False, assumptions=assumptions,
    )

    quantity = Decimal(qty)
    direction = Decimal("1") if side == PositionSide.LONG else Decimal("-1")
    gross_pnl = ((exit_reference - entry_reference) * quantity * direction).quantize(
        MONEY_QUANTUM, rounding=ROUND_HALF_UP,
    )
    fill_pnl = ((exit_fill - entry_fill) * quantity * direction).quantize(
        MONEY_QUANTUM, rounding=ROUND_HALF_UP,
    )
    slippage_cost = max(Decimal("0"), gross_pnl - fill_pnl).quantize(MONEY_QUANTUM)

    turnover = (entry_fill + exit_fill) * quantity
    fees = (turnover * assumptions.fees_bps_per_side / TEN_THOUSAND).quantize(
        MONEY_QUANTUM, rounding=ROUND_CEILING,
    )
    total_costs = slippage_cost + fees
    net_pnl = gross_pnl - total_costs

    return PaperSettlement(
        entry_fill_price=entry_fill,
        exit_fill_price=exit_fill,
        gross_pnl=gross_pnl,
        slippage_cost=slippage_cost,
        fees=fees,
        total_costs=total_costs,
        net_pnl=net_pnl,
    )


def cap_quantity_to_reference_stop_risk(
    *,
    entry_reference_price: Decimal,
    stop_reference_price: Decimal,
    requested_qty: int,
    side: str,
    assumptions: PaperCostAssumptions,
    entry_fill_price: Decimal,
    lot_size: int = 1,
    risk_budget: Decimal | None = None,
) -> int:
    """
    Keep modeled stop loss within the risk amount used to size the signal.

    Paper execution finds the largest (lot-aligned) quantity whose net loss at
    the stop still fits the supplied account risk budget. If a caller omits the
    budget, the reference-risk amount is used for a conservative standalone
    calculation. The binary search handles fee cent-rounding without assuming
    perfectly linear costs.
    """
    entry_reference = _decimal(entry_reference_price, "entry_reference_price")
    stop_reference = _decimal(stop_reference_price, "stop_reference_price")
    if not isinstance(requested_qty, int) or isinstance(requested_qty, bool) or requested_qty <= 0:
        raise ValueError("requested_qty must be a positive integer.")
    if not isinstance(lot_size, int) or isinstance(lot_size, bool) or lot_size <= 0:
        raise ValueError("lot_size must be a positive integer.")

    stop_distance = abs(entry_reference - stop_reference)
    if stop_distance <= 0:
        return 0
    if risk_budget is None:
        validated_risk_budget = stop_distance * Decimal(requested_qty)
    else:
        validated_risk_budget = _decimal(risk_budget, "risk_budget")
        if validated_risk_budget <= 0:
            return 0
    max_lots = requested_qty // lot_size
    low, high = 0, max_lots

    while low < high:
        candidate_lots = (low + high + 1) // 2
        candidate_qty = candidate_lots * lot_size
        settlement = calculate_paper_settlement(
            entry_reference_price=entry_reference,
            exit_reference_price=stop_reference,
            qty=candidate_qty,
            side=side,
            assumptions=assumptions,
            entry_fill_price=entry_fill_price,
        )
        modeled_loss = max(Decimal("0"), -settlement.net_pnl)
        if modeled_loss <= validated_risk_budget:
            low = candidate_lots
        else:
            high = candidate_lots - 1

    return low * lot_size
