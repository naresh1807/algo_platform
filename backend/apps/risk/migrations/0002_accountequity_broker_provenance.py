from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("risk", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="accountequity",
            name="source_mode",
            field=models.CharField(
                choices=[("paper", "Paper"), ("live", "Live")],
                default="paper",
                help_text="Whether the current risk baseline is simulated or broker-synchronized.",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="accountequity",
            name="last_broker_sync_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
