"""
python manage.py run_backtest --symbol NIFTY --timeframe 5m

Thin CLI wrapper around apps.analytics.backtest.walk_forward_backtest --
see that module's docstring for scope (technical layer only) and the
chronological train/test split it performs. Read-only: this never
writes a TradingSignal, StrategyVersion, or any other live-system row --
it only reads HistoricalData and prints a report, so it's always safe
to run against the real database, including a live one, at any time.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.analytics.backtest import (
    DEFAULT_FEES_BPS_PER_SIDE,
    DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    walk_forward_backtest,
)


class Command(BaseCommand):
    help = "Walk-forward backtest of the technical entry/exit layer against stored historical candles."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", required=True)
        parser.add_argument("--timeframe", default="5m")
        parser.add_argument(
            "--thresholds", default="0.5,0.6,0.7,0.8",
            help="Comma-separated technical_score_threshold values to grid-search.",
        )
        parser.add_argument(
            "--atr-multipliers", default="1.2,1.5,1.8",
            help="Comma-separated atr_stop_multiplier values to grid-search.",
        )
        parser.add_argument(
            "--test-fraction", type=float, default=0.3,
            help="Fraction of history (most recent, chronologically) held out as the out-of-sample test slice.",
        )
        parser.add_argument(
            "--slippage-bps-per-side", type=float, default=DEFAULT_SLIPPAGE_BPS_PER_SIDE,
            help=(
                "Adverse fill slippage in basis points on each entry and exit "
                f"(default: {DEFAULT_SLIPPAGE_BPS_PER_SIDE}; use 0 with --fees-bps-per-side=0 "
                "for a frictionless comparison)."
            ),
        )
        parser.add_argument(
            "--fees-bps-per-side", type=float, default=DEFAULT_FEES_BPS_PER_SIDE,
            help=(
                "Aggregate brokerage, taxes, and exchange charges in basis points "
                f"on each entry and exit (default: {DEFAULT_FEES_BPS_PER_SIDE})."
            ),
        )
        parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a formatted report.")

    def handle(self, *args, **options):
        thresholds = [float(x) for x in options["thresholds"].split(",")]
        multipliers = [float(x) for x in options["atr_multipliers"].split(",")]

        try:
            result = walk_forward_backtest(
                options["symbol"], options["timeframe"],
                technical_score_thresholds=thresholds,
                atr_stop_multipliers=multipliers,
                test_fraction=options["test_fraction"],
                slippage_bps_per_side=options["slippage_bps_per_side"],
                fees_bps_per_side=options["fees_bps_per_side"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if options["json"]:
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return

        if "error" in result:
            self.stderr.write(self.style.ERROR(f"Backtest could not run: {result['error']}"))
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return

        self.stdout.write(self.style.SUCCESS(
            f"\n{result['symbol']} {result['timeframe']} -- "
            f"train={result['train_candles']} candles, test={result['test_candles']} candles\n"
        ))
        cost_model = result["cost_model"]
        self.stdout.write(
            "Cost model (each side): "
            f"slippage={cost_model['slippage_bps_per_side']} bps, "
            f"fees={cost_model['fees_bps_per_side']} bps; "
            f"round-trip drag={cost_model['assumed_round_trip_drag_bps']} bps.\n"
        )
        self.stdout.write(
            f"Selected on train slice: technical_score_threshold="
            f"{result['selected_technical_score_threshold']}, "
            f"atr_stop_multiplier={result['selected_atr_stop_multiplier']}\n"
        )
        train, test = result["train_metrics"], result["test_metrics"]
        self.stdout.write(
            f"  train: {train['total_trades']} trades, win_rate={train['win_rate']}, "
            f"gross_expectancy_r={train['gross_expectancy_r']}, "
            f"net_expectancy_r={train['expectancy_r']}, profit_factor={train['profit_factor']}\n"
        )
        self.stdout.write(
            f"  test:  {test['total_trades']} trades, win_rate={test['win_rate']}, "
            f"gross_expectancy_r={test['gross_expectancy_r']}, "
            f"net_expectancy_r={test['expectancy_r']}, profit_factor={test['profit_factor']}\n"
        )
        self.stdout.write(self.style.WARNING(f"\n{result['note']}\n"))
