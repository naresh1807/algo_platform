import json
from django.core.management.base import BaseCommand, CommandError
from apps.learning.scalp_return_training import train_returns


class Command(BaseCommand):
    help = "Train an inactive net-return selection model using a strategy dataset."
    def add_arguments(self, parser):
        parser.add_argument("--dataset",required=True)
        parser.add_argument("--output-dir",required=True)
    def handle(self,*args,**options):
        try:
            result=train_returns(options["dataset"],options["output_dir"])
        except ValueError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps(result,indent=2))
