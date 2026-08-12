from django.db import models


class FeedHealthCheck(models.Model):
    """
    manual section 17: Monitoring and Observability. A lightweight
    heartbeat table -- a Celery task pings the broker feed periodically
    and writes a row here; apps.execution's pre-trade validation
    (manual section 16: "Validate feed freshness") reads the latest row
    before allowing any order, rather than pinging the broker inline on
    every single trade decision.
    """

    checked_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=64, help_text="e.g. angel_one_ws, nse_historical")
    is_healthy = models.BooleanField()
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    detail = models.TextField(blank=True)

    class Meta:
        db_table = "feed_health_checks"
        ordering = ["-checked_at"]
        indexes = [models.Index(fields=["source", "-checked_at"])]

    def __str__(self):
        return f"{self.source}: {'OK' if self.is_healthy else 'DOWN'} @ {self.checked_at}"


class PriceAlert(models.Model):
    """
    A trader-set watch on a symbol crossing a target price -- a
    standard feature of any real trading platform, not something the
    manual specifies (this app's own manual chapter never mentions
    price alerts) but a genuine gap for a "meets a trader's actual
    needs" platform. Checked by
    apps.monitoring.tasks.check_price_alerts against real
    HistoricalData candles; a triggered alert fires a JARVIS
    announcement (apps.jarvis.notify.announce, kind="price_alert") the
    same way every other proactive notification in this platform does,
    rather than needing its own separate delivery mechanism.

    One-shot by design: once triggered, `active` flips to False rather
    than re-firing on every subsequent candle that still satisfies the
    condition -- a trader wants to know a level was crossed, not get
    the same alert every 5 minutes forever. Re-arming (if the trader
    wants to watch the same level again) is deleting and recreating
    the alert, kept intentionally simple rather than adding a
    snooze/rearm state machine.
    """

    class Condition(models.TextChoices):
        ABOVE = "above", "Crosses above"
        BELOW = "below", "Crosses below"

    symbol = models.CharField(max_length=32, db_index=True)
    condition = models.CharField(max_length=8, choices=Condition.choices)
    target_price = models.DecimalField(max_digits=14, decimal_places=4)
    created_by = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, null=True, blank=True, related_name="price_alerts",
    )
    active = models.BooleanField(default=True, help_text="False once triggered (or manually cancelled).")
    created_at = models.DateTimeField(auto_now_add=True)
    triggered_at = models.DateTimeField(null=True, blank=True)
    triggered_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)

    class Meta:
        db_table = "price_alerts"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["symbol", "active"])]

    def __str__(self):
        state = "triggered" if self.triggered_at else ("active" if self.active else "cancelled")
        return f"{self.symbol} {self.condition} {self.target_price} [{state}]"
