from django.db import models

from common.constants import RiskEventSeverity


class AccountEquity(models.Model):
    """
    Singleton (like KillSwitchState) tracking the numbers every risk
    check in engine.py needs: current equity (for position sizing and
    exposure %), the day's starting equity (for the daily-loss-limit
    check), the all-time peak (for drawdown %), and the current losing
    streak (for the consecutive-losses check).

    This is deliberately its own row rather than derived from summing
    OpenPosition.unrealized_pnl on every check -- risk checks run before
    every single order (manual section 16), so they need one cheap
    lookup, not an aggregate query over the whole position history.
    apps.execution is responsible for keeping this updated as trades
    open/close and P&L moves (not yet implemented).
    """

    current_equity = models.DecimalField(max_digits=16, decimal_places=2)
    daily_start_equity = models.DecimalField(max_digits=16, decimal_places=2)
    peak_equity = models.DecimalField(max_digits=16, decimal_places=2)
    consecutive_losses = models.PositiveSmallIntegerField(default=0)
    trading_day = models.DateField(help_text="Which trading day daily_start_equity refers to")
    source_mode = models.CharField(
        max_length=8,
        choices=[("paper", "Paper"), ("live", "Live")],
        default="paper",
        help_text="Whether the current risk baseline is simulated or broker-synchronized.",
    )
    last_broker_sync_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "account_equity"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton, same pattern as KillSwitchState
        super().save(*args, **kwargs)

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return float((self.peak_equity - self.current_equity) / self.peak_equity * 100)

    @property
    def daily_pnl_pct(self) -> float:
        if self.daily_start_equity <= 0:
            return 0.0
        return float((self.current_equity - self.daily_start_equity) / self.daily_start_equity * 100)

    def __str__(self):
        return f"Equity {self.current_equity} (DD {self.drawdown_pct:.1f}%)"


class EquitySnapshot(models.Model):
    """
    A time-series point-in-time record of AccountEquity.current_equity,
    written automatically (apps/risk/signals.py) every time it changes.

    This is what closes the real gap apps.analytics.services.py's
    compute_daily_performance docstring documents honestly: a true
    intraday max-drawdown (and a Sharpe ratio across days) needs an
    equity CURVE sampled through time, not just closed-trade P&L --
    AccountEquity itself only ever holds the current/peak/daily-start
    numbers, never history. Deliberately its own append-only table
    (never updated, only inserted) so a later change to how equity is
    computed can't silently rewrite what past drawdown actually looked
    like at the time.
    """

    timestamp = models.DateTimeField(db_index=True)
    equity = models.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        db_table = "equity_snapshots"
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["-timestamp"])]

    def __str__(self):
        return f"{self.equity} @ {self.timestamp}"


class RiskEvent(models.Model):
    """
    manual section 7: risk_events. Every time the risk engine blocks a
    trade, reduces size, or trips the kill switch, it logs a row here --
    this table IS the audit trail the manual's "Non-Negotiable Rules"
    (section 21) and "Every decision must be logged" principle require.

    severity=CRITICAL is what apps/risk/killswitch.py (not yet written)
    watches for: a CRITICAL row is what actually flips the kill switch,
    not just a threshold check inline in the execution path -- so the
    kill-switch state is always reconstructable from this table alone,
    even after a process restart.
    """

    symbol = models.CharField(max_length=32, blank=True, db_index=True)
    event_type = models.CharField(
        max_length=64,
        help_text="e.g. drawdown_breach, stale_feed, broker_mismatch, "
                  "daily_loss_limit, exposure_limit, kill_switch_triggered",
    )
    message = models.TextField()
    severity = models.CharField(max_length=16, choices=RiskEventSeverity.choices)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "risk_events"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["severity", "-created_at"])]

    def __str__(self):
        return f"[{self.severity}] {self.event_type} ({self.symbol or 'PORTFOLIO'})"


class KillSwitchState(models.Model):
    """
    Singleton-style table (always exactly one row, enforced by save())
    holding the current kill-switch state. Deliberately NOT computed
    on-the-fly from RiskEvent rows on every request -- the execution
    path needs to check this with a single cheap lookup before every
    order, so it's kept as durable, explicitly-set state rather than
    derived each time.
    """

    is_active = models.BooleanField(default=False)
    triggered_by_event = models.ForeignKey(
        RiskEvent, null=True, blank=True, on_delete=models.SET_NULL,
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivated_by = models.CharField(
        max_length=150, blank=True,
        help_text="Manual re-arming must always be a deliberate human action "
                  "per the manual's safety rules -- record who did it.",
    )

    class Meta:
        db_table = "kill_switch_state"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)

    def __str__(self):
        return "ACTIVE" if self.is_active else "inactive"
