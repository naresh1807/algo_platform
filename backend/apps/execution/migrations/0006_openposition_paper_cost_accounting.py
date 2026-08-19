# Generated manually to keep the paper-cost data backfill explicit and auditable.

from decimal import Decimal

from django.db import migrations, models


def backfill_legacy_position_accounting(apps, schema_editor):
    """Preserve historical P&L and treat legacy paper trades as zero-cost."""
    from django.db.models import F

    OpenPosition = apps.get_model("execution", "OpenPosition")
    OpenPosition.objects.update(entry_reference_price=F("entry_price"))
    OpenPosition.objects.filter(execution_mode="paper").update(
        paper_slippage_bps_per_side=Decimal("0"),
        paper_fees_bps_per_side=Decimal("0"),
    )
    OpenPosition.objects.filter(closed_at__isnull=False).update(
        gross_realized_pnl=F("unrealized_pnl"),
        realized_pnl=F("unrealized_pnl"),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("execution", "0005_brokerorder_openposition_signal_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="openposition",
            name="entry_reference_price",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                help_text="Pre-slippage entry price used to audit paper-trade gross P&L.",
                max_digits=14,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="exit_price",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                help_text="Executed exit fill price after paper slippage, or a live broker fill.",
                max_digits=14,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="exit_reference_price",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                help_text="Pre-slippage market/trigger price that caused the paper exit.",
                max_digits=14,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="fees",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Round-trip modeled brokerage, taxes, and exchange fees.",
                max_digits=16,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="gross_realized_pnl",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Closed-trade P&L at reference prices before slippage and fees.",
                max_digits=16,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="last_mark_price",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                help_text="Latest pre-slippage reference price used for paper mark-to-market.",
                max_digits=14,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="paper_fees_bps_per_side",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                help_text="Paper aggregate fee assumption snapshotted at open; null for live positions.",
                max_digits=8,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="paper_slippage_bps_per_side",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                help_text="Paper slippage assumption snapshotted at open; null for live positions.",
                max_digits=8,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="realized_pnl",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Net closed-trade P&L after all modeled or broker-reported costs.",
                max_digits=16,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="slippage_cost",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Round-trip paper slippage drag in currency units.",
                max_digits=16,
            ),
        ),
        migrations.AddField(
            model_name="openposition",
            name="total_costs",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="slippage_cost + fees for the closed trade.",
                max_digits=16,
            ),
        ),
        migrations.AlterField(
            model_name="openposition",
            name="entry_price",
            field=models.DecimalField(
                decimal_places=4,
                help_text=(
                    "Executed entry fill price. For paper positions this includes the "
                    "snapshotted adverse slippage; entry_reference_price retains the "
                    "pre-cost signal price."
                ),
                max_digits=14,
            ),
        ),
        migrations.AlterField(
            model_name="openposition",
            name="unrealized_pnl",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text=(
                    "Conservative net liquidation P&L while open; retained as net realized "
                    "P&L after close for backward compatibility with analytics/risk callers."
                ),
                max_digits=14,
            ),
        ),
        migrations.RunPython(
            backfill_legacy_position_accounting,
            migrations.RunPython.noop,
        ),
    ]
