from django.db import models


class HistoricalData(models.Model):
    """
    OHLCV candle storage (manual section 7: historical_data).

    unique_together on (symbol, timeframe, timestamp) is the key design
    decision here: it makes re-ingesting the same broker/feed data an
    idempotent operation (INSERT ... ON CONFLICT-style upsert) instead of
    silently creating duplicate candles if a data-ingestion job is re-run
    after a crash.
    """

    symbol = models.CharField(max_length=32, db_index=True)
    timeframe = models.CharField(max_length=8, help_text="e.g. 1m, 5m, 10m, 15m, 30m, 1h, 1d (see broker_client.TIMEFRAME_TO_ANGEL_INTERVAL)")
    timestamp = models.DateTimeField(db_index=True)
    open = models.DecimalField(max_digits=14, decimal_places=4)
    high = models.DecimalField(max_digits=14, decimal_places=4)
    low = models.DecimalField(max_digits=14, decimal_places=4)
    close = models.DecimalField(max_digits=14, decimal_places=4)
    volume = models.BigIntegerField(default=0)
    source = models.CharField(max_length=64, help_text="e.g. angel_one, nse_historical")

    class Meta:
        db_table = "historical_data"
        unique_together = ("symbol", "timeframe", "timestamp")
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["symbol", "timeframe", "-timestamp"]),
        ]

    def __str__(self):
        return f"{self.symbol} {self.timeframe} @ {self.timestamp}"
