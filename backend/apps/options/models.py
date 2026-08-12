"""
manual section 9: "Options Intelligence Manual". Two tables, matching
the same "static reference data" vs. "time-series snapshot" split
apps.market_data uses (compare: nothing static like HistoricalData's
symbol/timeframe combo needing its own table -- but here, WHICH
contracts exist for a given underlying+expiry is itself semi-static
data worth storing separately from the fast-changing OI/IV/volume
numbers, since re-deriving strike lists from Angel One's instrument
master on every snapshot would be wasteful).
"""

from django.db import models


class OptionContract(models.Model):
    """
    One row per (underlying, expiry, strike, option_type) -- e.g. one
    row for "NIFTY 24500 CE expiring 2026-08-07". This almost never
    changes once an expiry's contracts are listed (Angel One doesn't
    add new strikes to an already-listed expiry mid-week in practice),
    so it's refreshed periodically (apps/options/tasks.py), not on
    every chain snapshot.
    """

    class OptionType(models.TextChoices):
        CALL = "CE", "Call"
        PUT = "PE", "Put"

    underlying = models.CharField(max_length=32, db_index=True, help_text="e.g. NIFTY, BANKNIFTY")
    expiry = models.DateField(db_index=True)
    strike = models.DecimalField(max_digits=12, decimal_places=2)
    option_type = models.CharField(max_length=2, choices=OptionType.choices)
    symbol_token = models.CharField(
        max_length=32, unique=True,
        help_text="Broker's symboltoken for this specific contract -- needed to fetch quotes for it.",
    )

    class Meta:
        db_table = "option_contracts"
        unique_together = ("underlying", "expiry", "strike", "option_type")
        ordering = ["underlying", "expiry", "strike"]
        indexes = [models.Index(fields=["underlying", "expiry"])]

    def __str__(self):
        return f"{self.underlying} {self.strike} {self.option_type} {self.expiry}"


class OptionChainSnapshot(models.Model):
    """
    One row per (contract, timestamp) -- the actual time-series data:
    OI, change in OI, volume, IV, LTP, bid/ask. This is what
    apps.options.metrics computes PCR/max-pain/IV-rank from, and what
    apps.options.signals_engine reads to detect buildup/unwinding
    patterns (which need to compare THIS snapshot against the previous
    one for the same contract, not just look at a single point in time).
    """

    contract = models.ForeignKey(OptionContract, on_delete=models.CASCADE, related_name="snapshots")
    timestamp = models.DateTimeField(db_index=True)
    ltp = models.DecimalField(max_digits=12, decimal_places=2)
    open_interest = models.BigIntegerField()
    change_in_oi = models.BigIntegerField(
        help_text="OI change vs. the previous snapshot for this contract -- "
                  "computed at ingestion time, not derived on every read.",
    )
    volume = models.BigIntegerField(default=0)
    iv = models.FloatField(null=True, blank=True, help_text="Implied volatility, as a percentage")
    bid = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    ask = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "option_chain_snapshots"
        unique_together = ("contract", "timestamp")
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["contract", "-timestamp"])]

    def __str__(self):
        return f"{self.contract} @ {self.timestamp}: OI={self.open_interest}"
