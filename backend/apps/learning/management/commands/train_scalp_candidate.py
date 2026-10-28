import json
from django.core.management.base import BaseCommand, CommandError
from apps.learning.scalp_candidate import train_candidate


class Command(BaseCommand):
    help = "Train an inactive scalping candidate with purged whole-day validation/test splits."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", required=True)
        parser.add_argument("--threshold", type=float, default=.6)

    def handle(self, *args, **options):
        if not 0 < options["threshold"] < 1:
            raise CommandError("Threshold must be between zero and one.")
        try:
            result = train_candidate(options["output_dir"], options["threshold"])
        except ValueError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps(result, indent=2))
