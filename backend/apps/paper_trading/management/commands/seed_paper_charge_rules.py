from datetime import date

from django.core.management.base import BaseCommand

from apps.paper_trading.models import PaperChargeRuleSet

# HONESTY NOTE (spec: "Do not guess missing rates. Centralize rates...
# and document how they must be updated."): these are commonly-cited
# APPROXIMATE NSE F&O options charge components (flat discount-broker
# brokerage, STT/exchange/SEBI/GST/stamp-duty rates as commonly
# published for index options), NOT fetched from any official current
# circular. SEBI/exchange rates change periodically (STT in particular
# has changed more than once in recent years) -- before treating this
# subsystem's charge/net-P&L figures as financially accurate, a human
# must verify these seven rates against the current official NSE/SEBI/
# CBDT circulars and either edit this seed (before first run) or insert
# a NEW PaperChargeRuleSet row with a later effective_from (never edit
# an already-used row -- see PaperChargeRuleSet's own docstring).
DEFAULT_VERSION = "v1_approximate_2026"
DEFAULT_EFFECTIVE_FROM = date(2026, 1, 1)
DEFAULT_RATES = dict(
    brokerage_flat_per_order="20.00",
    stt_pct_of_premium="0.0625",
    exchange_txn_pct="0.03503",
    gst_pct_of_brokerage_and_txn="18.0",
    sebi_charges_pct="0.0001",
    stamp_duty_pct_buy_side="0.003",
    other_charges_flat_per_order="0.00",
)


class Command(BaseCommand):
    help = (
        "Seeds the initial PaperChargeRuleSet row required before apps.paper_trading "
        "can settle any trade. Safe to re-run (does nothing if a row with this "
        "version already exists). See this file's own module-level note on why "
        "these rates are documented approximations, not guaranteed-current official figures."
    )

    def handle(self, *args, **options):
        _, created = PaperChargeRuleSet.objects.get_or_create(
            version=DEFAULT_VERSION,
            defaults={
                "effective_from": DEFAULT_EFFECTIVE_FROM,
                "notes": (
                    "Approximate NSE index-options charge structure -- verify against "
                    "current official rates before relying on net P&L figures. See this "
                    "command's module docstring."
                ),
                **DEFAULT_RATES,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created PaperChargeRuleSet {DEFAULT_VERSION!r}."))
        else:
            self.stdout.write(f"PaperChargeRuleSet {DEFAULT_VERSION!r} already exists -- no change made.")
