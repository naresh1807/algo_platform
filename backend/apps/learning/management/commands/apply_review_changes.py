"""
Turns an APPROVED DailyReviewNote's suggested_changes into a brand new
StrategyVersion -- deliberately a management command (same
manual-only-safety-action pattern as rearm_kill_switch), and
deliberately does NOT set active_flag=True on the new version.

This is "controlled updates" (manual Phase 5) as three separate,
explicit steps, each requiring its own deliberate action:
  1. run_daily_review (automatic) drafts suggested_changes.
  2. A human sets DailyReviewNote.approved_flag=True (via the API/admin
     -- this is the human "yes, these suggestions look reasonable").
  3. A human runs THIS command (via shell -- a second, separate,
     higher-friction action) to actually materialize those suggestions
     into a new StrategyVersion.
  4. Promoting that new version to active (active_flag=True) is a
     FOURTH, still separate action via the StrategyVersion API/admin,
     ideally only after paper-trading it for a while first.

Four separate gates for one parameter change might look excessive, but
manual section 21 is explicit: "Never auto-change core safety rules"
and "Never deploy unvalidated changes" -- friction here is the point.

Usage:
    python manage.py apply_review_changes 2026-08-01
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Create a new (inactive) StrategyVersion from an approved DailyReviewNote's suggested_changes."

    def add_arguments(self, parser):
        parser.add_argument("review_date", help="ISO date of the review to apply, e.g. 2026-08-01")

    def handle(self, *args, **options):
        from apps.learning.models import DailyReviewNote, StrategyVersion

        try:
            review = DailyReviewNote.objects.get(review_date=options["review_date"])
        except DailyReviewNote.DoesNotExist:
            raise CommandError(f"No DailyReviewNote for {options['review_date']}.")

        if not review.approved_flag:
            raise CommandError(
                f"Review for {review.review_date} is not approved yet -- "
                f"set approved_flag=True (via the API/admin) before applying it."
            )

        if not review.suggested_changes:
            self.stdout.write(self.style.WARNING("This review has no suggested_changes -- nothing to apply."))
            return

        current_active = StrategyVersion.objects.filter(active_flag=True).first()
        base_params = dict(current_active.params_json) if current_active else {}

        # suggested_changes is keyed like "signals_engine.TECHNICAL_SCORE_THRESHOLD"
        # with a {"current", "suggested", "why"} dict -- fold just the
        # "suggested" values into the new version's params_json, under
        # the same dotted key, so it's traceable back to exactly which
        # review recommended it.
        new_params = dict(base_params)
        applied = {}
        for key, change in review.suggested_changes.items():
            new_params[key] = change.get("suggested")
            applied[key] = change.get("suggested")

        # Preserve the outgoing version's baseline_metrics if it has
        # one, so drift detection has something to compare the NEW
        # version against too, once it's promoted and has its own trade
        # history -- otherwise a freshly-promoted version would have no
        # baseline at all and check_for_drift would just skip it forever.
        if current_active and "baseline_metrics" in current_active.params_json:
            new_params["baseline_metrics"] = current_active.params_json["baseline_metrics"]

        version_name = f"auto-{review.review_date.isoformat()}"
        if StrategyVersion.objects.filter(version_name=version_name).exists():
            raise CommandError(
                f"A StrategyVersion named {version_name!r} already exists -- "
                f"has this review already been applied?"
            )

        new_version = StrategyVersion.objects.create(
            version_name=version_name,
            params_json=new_params,
            active_flag=False,  # NOT promoted -- see module docstring
        )

        from apps.admin_tools.audit import log_action
        log_action(
            action="strategy_version_created_from_review",
            actor_label="management_command:apply_review_changes",
            target=new_version,
            details={"source_review_date": str(review.review_date), "applied_changes": applied},
        )

        self.stdout.write(self.style.SUCCESS(
            f"Created StrategyVersion '{new_version.version_name}' (id={new_version.pk}), "
            f"NOT active. Applied changes: {applied}. "
            f"Promote it explicitly (active_flag=True via the API/admin) when ready."
        ))
