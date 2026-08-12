from django.db import models

from common.constants import PositionSide


class OpenPosition(models.Model):
    """
    manual section 7: open_positions. `unrealized_pnl` is stored (not
    only computed on read) because the real-time layer (Channels) pushes
    P&L updates to the dashboard on every price tick -- persisting the
    last-known value means a freshly-connected dashboard client can show
    a correct number immediately, before the next tick arrives.

    signal is a ForeignKey (not just a symbol string) so a position can
    always be traced back to the exact TradingSignal + strategy_version
    that produced it -- required for the daily review and for any
    post-mortem after a losing trade.
    """

    signal = models.ForeignKey(
        "signals.TradingSignal", on_delete=models.PROTECT, related_name="positions",
    )
    symbol = models.CharField(max_length=32, db_index=True)
    side = models.CharField(max_length=8, choices=PositionSide.choices)
    qty = models.PositiveIntegerField()
    entry_price = models.DecimalField(max_digits=14, decimal_places=4)
    stop_loss = models.DecimalField(max_digits=14, decimal_places=4)
    target_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    trailing_stop_distance = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True,
        help_text=(
            "If set, apps.execution.trailing_stop ratchets stop_loss up as "
            "peak_price makes new highs, trailing by this distance. Set at "
            "open time from (entry_price - stop_loss) when "
            "settings.TRAILING_STOP_ENABLED -- null means trailing is off "
            "for this position (the pre-trailing-stop default)."
        ),
    )
    peak_price = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True,
        help_text="Highest price seen since entry -- only tracked when trailing_stop_distance is set.",
    )
    unrealized_pnl = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "open_positions"
        ordering = ["-opened_at"]
        indexes = [models.Index(fields=["symbol"]), models.Index(fields=["closed_at"])]

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def __str__(self):
        state = "OPEN" if self.is_open else "CLOSED"
        return f"{self.symbol} {self.side} x{self.qty} [{state}]"
