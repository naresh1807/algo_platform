from django.db import models


class PerformanceMetrics(models.Model):
    """
    manual section 7: performance_metrics. One row per trading day.
    Computed by a Celery task after market close (the same job that
    feeds apps.learning.DailyReviewNote) -- stored rather than
    recomputed on every dashboard load, since these are backward-looking
    numbers that never change once the day is over.
    """

    date = models.DateField(unique=True)
    total_trades = models.PositiveIntegerField(default=0)
    win_rate = models.FloatField(null=True, blank=True, help_text="0.0-1.0")
    profit_factor = models.FloatField(null=True, blank=True)
    expectancy = models.FloatField(null=True, blank=True, help_text="Average R per trade")
    avg_r = models.FloatField(null=True, blank=True)
    max_drawdown = models.FloatField(null=True, blank=True, help_text="As a % of equity")
    slippage = models.FloatField(null=True, blank=True, help_text="Average slippage, in price units")
    false_signal_rate = models.FloatField(null=True, blank=True, help_text="0.0-1.0")

    class Meta:
        db_table = "performance_metrics"
        ordering = ["-date"]

    def __str__(self):
        return f"Performance {self.date}"
