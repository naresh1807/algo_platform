"""Database-free tests for deterministic paper fill/cost arithmetic."""

from decimal import Decimal
from unittest import TestCase

from common.constants import PositionSide

from .paper_costs import (
    PaperCostAssumptions,
    adverse_fill_price,
    cap_quantity_to_reference_stop_risk,
    calculate_paper_settlement,
)


class PaperCostCalculationTests(TestCase):
    def setUp(self):
        self.assumptions = PaperCostAssumptions(
            slippage_bps_per_side=Decimal("10"),
            fees_bps_per_side=Decimal("5"),
        )

    def test_long_round_trip_is_adverse_and_reconciles_gross_to_net(self):
        result = calculate_paper_settlement(
            entry_reference_price=Decimal("100"),
            exit_reference_price=Decimal("110"),
            qty=10,
            side=PositionSide.LONG,
            assumptions=self.assumptions,
        )

        self.assertEqual(result.entry_fill_price, Decimal("100.1000"))
        self.assertEqual(result.exit_fill_price, Decimal("109.8900"))
        self.assertEqual(result.gross_pnl, Decimal("100.00"))
        self.assertEqual(result.slippage_cost, Decimal("2.10"))
        self.assertEqual(result.fees, Decimal("1.05"))
        self.assertEqual(result.total_costs, Decimal("3.15"))
        self.assertEqual(result.net_pnl, Decimal("96.85"))
        self.assertEqual(result.gross_pnl - result.total_costs, result.net_pnl)

    def test_short_entry_and_exit_slip_in_the_adverse_directions(self):
        result = calculate_paper_settlement(
            entry_reference_price=Decimal("100"),
            exit_reference_price=Decimal("90"),
            qty=10,
            side=PositionSide.SHORT,
            assumptions=self.assumptions,
        )

        self.assertEqual(result.entry_fill_price, Decimal("99.9000"))
        self.assertEqual(result.exit_fill_price, Decimal("90.0900"))
        self.assertEqual(result.gross_pnl, Decimal("100.00"))
        self.assertEqual(result.slippage_cost, Decimal("1.90"))
        self.assertEqual(result.fees, Decimal("0.95"))
        self.assertEqual(result.net_pnl, Decimal("97.15"))

    def test_zero_cost_mode_exactly_preserves_reference_pnl(self):
        result = calculate_paper_settlement(
            entry_reference_price=Decimal("100"),
            exit_reference_price=Decimal("110"),
            qty=10,
            side=PositionSide.LONG,
            assumptions=PaperCostAssumptions(Decimal("0"), Decimal("0")),
        )

        self.assertEqual(result.entry_fill_price, Decimal("100.0000"))
        self.assertEqual(result.exit_fill_price, Decimal("110.0000"))
        self.assertEqual(result.gross_pnl, Decimal("100.00"))
        self.assertEqual(result.total_costs, Decimal("0.00"))
        self.assertEqual(result.net_pnl, Decimal("100.00"))

    def test_option_style_assumptions_create_more_drag_than_cash(self):
        cash = PaperCostAssumptions(Decimal("5"), Decimal("5"))
        option = PaperCostAssumptions(Decimal("25"), Decimal("10"))
        inputs = {
            "entry_reference_price": Decimal("100"),
            "exit_reference_price": Decimal("110"),
            "qty": 25,
            "side": PositionSide.LONG,
        }

        cash_result = calculate_paper_settlement(**inputs, assumptions=cash)
        option_result = calculate_paper_settlement(**inputs, assumptions=option)

        self.assertGreater(option_result.total_costs, cash_result.total_costs)
        self.assertLess(option_result.net_pnl, cash_result.net_pnl)

    def test_buy_rounds_up_and_sell_rounds_down_to_avoid_optimism(self):
        assumptions = PaperCostAssumptions(Decimal("1"), Decimal("0"))

        buy = adverse_fill_price(
            Decimal("1.00001"), side=PositionSide.LONG,
            is_entry=True, assumptions=assumptions,
        )
        sell = adverse_fill_price(
            Decimal("1.00001"), side=PositionSide.LONG,
            is_entry=False, assumptions=assumptions,
        )

        self.assertEqual(buy, Decimal("1.0002"))
        self.assertEqual(sell, Decimal("0.9999"))

    def test_invalid_assumptions_and_quantity_are_rejected(self):
        with self.assertRaises(ValueError):
            PaperCostAssumptions(Decimal("-1"), Decimal("0"))
        with self.assertRaises(ValueError):
            calculate_paper_settlement(
                entry_reference_price=Decimal("100"),
                exit_reference_price=Decimal("110"),
                qty=0,
                side=PositionSide.LONG,
                assumptions=self.assumptions,
            )

    def test_cost_aware_quantity_cap_keeps_stop_loss_within_hard_budget(self):
        entry_fill = adverse_fill_price(
            Decimal("100"), side=PositionSide.LONG,
            is_entry=True, assumptions=self.assumptions,
        )
        capped = cap_quantity_to_reference_stop_risk(
            entry_reference_price=Decimal("100"),
            stop_reference_price=Decimal("95"),
            requested_qty=200,
            side=PositionSide.LONG,
            assumptions=self.assumptions,
            entry_fill_price=entry_fill,
            risk_budget=Decimal("1000"),
        )

        self.assertLess(capped, 200)
        at_cap = calculate_paper_settlement(
            entry_reference_price=Decimal("100"), exit_reference_price=Decimal("95"),
            qty=capped, side=PositionSide.LONG, assumptions=self.assumptions,
            entry_fill_price=entry_fill,
        )
        above_cap = calculate_paper_settlement(
            entry_reference_price=Decimal("100"), exit_reference_price=Decimal("95"),
            qty=capped + 1, side=PositionSide.LONG, assumptions=self.assumptions,
            entry_fill_price=entry_fill,
        )
        self.assertLessEqual(-at_cap.net_pnl, Decimal("1000"))
        self.assertGreater(-above_cap.net_pnl, Decimal("1000"))

    def test_cost_cap_preserves_option_lot_alignment(self):
        entry_fill = adverse_fill_price(
            Decimal("110"), side=PositionSide.LONG,
            is_entry=True, assumptions=self.assumptions,
        )
        capped = cap_quantity_to_reference_stop_risk(
            entry_reference_price=Decimal("110"),
            stop_reference_price=Decimal("95"),
            requested_qty=75,
            side=PositionSide.LONG,
            assumptions=self.assumptions,
            entry_fill_price=entry_fill,
            lot_size=25,
            risk_budget=Decimal("1000"),
        )

        self.assertEqual(capped % 25, 0)
        self.assertLessEqual(capped, 75)
