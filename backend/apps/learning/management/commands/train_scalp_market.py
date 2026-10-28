import json
from django.core.management.base import BaseCommand, CommandError
from apps.learning.scalp_market_training import run_market_training


class Command(BaseCommand):
    help = "Build labels from stored market candles and train an inactive scalping candidate."
    def add_arguments(self, parser):
        parser.add_argument("--output-dir", required=True)
        parser.add_argument("--strategy-setups", action="store_true")
        parser.add_argument("--limit",type=int,default=60000)
        parser.add_argument("--cost-r",type=float,default=.1)
    def handle(self,*args,**options):
        if options["limit"] < 1000 or options["cost_r"] < 0:
            raise CommandError("Use at least 1000 candles and non-negative cost.")
        try:
            report=run_market_training(options["output_dir"],limit=options["limit"],cost_r=options["cost_r"],strategy_setups=options["strategy_setups"])
        except ValueError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps(report,indent=2))
