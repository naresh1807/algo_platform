import uuid

from django.db import models

from common.constants import PositionSide


class ExecutionModeSetting(models.Model):
    """
    Singleton (always exactly one row, same enforced-pk=1 pattern as
    apps.risk.models.KillSwitchState) holding the operator-controlled
    override for settings.EXECUTION_MODE. apps.execution.tasks.
    run_trading_cycle reads this via get_execution_mode() below rather
    than the static settings.EXECUTION_MODE env var, so a change made
    from the frontend Settings page (apps.execution.views.
    ExecutionModeView) takes effect on the very next scheduled trading
    cycle -- no process restart needed.

    settings.EXECUTION_MODE (.env) is only ever consulted as this row's
    seed value the FIRST time it's created (see get_execution_mode) --
    same "paper is the safe default, live is a deliberate opt-in"
    posture that env var's own docstring in config/settings.py
    establishes, now made changeable at runtime through a UI that
    requires typed confirmation before allowing LIVE.
    """

    class Mode(models.TextChoices):
        PAPER = "paper", "Paper"
        LIVE = "live", "Live"

    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.PAPER)
    changed_at = models.DateTimeField(auto_now=True)
    changed_by = models.CharField(
        max_length=150, blank=True,
        help_text="Who last changed this -- switching to LIVE enables real order "
                  "placement with real money, so this is always attributed to a "
                  "specific logged-in user, same posture as KillSwitchState.deactivated_by.",
    )

    class Meta:
        db_table = "execution_mode_setting"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)

    def __str__(self):
        return self.mode


def get_execution_mode() -> str:
    """
    The live, DB-backed effective execution mode ("paper" or "live").
    Callers that decide whether to place real orders (currently only
    apps.execution.tasks.run_trading_cycle) must call this, never read
    settings.EXECUTION_MODE directly, or a Settings-page change
    wouldn't take effect until the next process restart.
    """
    from django.conf import settings

    row, _ = ExecutionModeSetting.objects.get_or_create(pk=1, defaults={"mode": settings.EXECUTION_MODE})
    return row.mode


class OpenPosition(models.Model):
    """
    manual section 7: open_positions. `unrealized_pnl` is stored (not
    only computed on read) because the real-time layer (Channels) pushes
    P&L updates to the dashboard on every price tick -- persisting the
    last-known value means a freshly-connected dashboard client can show
    a correct number immediately, before the next tick arrives.

    signal is a OneToOneField (not just a symbol string) so a position can
    always be traced back to the exact TradingSignal + strategy_version
    that produced it, while the database prevents a retried executor from
    opening the same signal twice.
    """

    signal = models.OneToOneField(
        "signals.TradingSignal", on_delete=models.PROTECT, related_name="positions",
    )
    option_contract = models.ForeignKey(
        "options.OptionContract", null=True, blank=True,
        on_delete=models.PROTECT, related_name="positions",
        help_text=(
            "Set only when this position is a real option-premium position (copied "
            "from signal.option_contract at open time, see paper_executor.open_position_"
            "from_signal) -- entry_price/stop_loss/target_price are OPTION PREMIUM, not "
            "index points, whenever this is set, and check_and_close_positions fetches "
            "this contract's own live LTP (apps.options.pricing.latest_ltp_for_contract) "
            "instead of compute_indicators(symbol), which has no candle history for an "
            "option contract's own symbol."
        ),
    )
    symbol = models.CharField(max_length=32, db_index=True)
    execution_mode = models.CharField(
        max_length=8,
        choices=ExecutionModeSetting.Mode.choices,
        default=ExecutionModeSetting.Mode.PAPER,
        db_index=True,
        help_text=(
            "Execution venue that opened this position; paper and live "
            "positions must never share an exit path."
        ),
    )
    timeframe = models.CharField(
        max_length=8,
        default="5m",
        db_index=True,
        help_text="Candle timeframe that owns this position's exit decisions.",
    )
    strategy_key = models.CharField(
        max_length=64,
        blank=True,
        help_text="Optional strategy owner used to keep concurrent execution loops isolated.",
    )
    side = models.CharField(max_length=8, choices=PositionSide.choices)
    qty = models.PositiveIntegerField()
    entry_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        help_text=(
            "Executed entry fill price. For paper positions this includes the "
            "snapshotted adverse slippage; entry_reference_price retains the "
            "pre-cost signal price."
        ),
    )
    entry_reference_price = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True,
        help_text="Pre-slippage entry price used to audit paper-trade gross P&L.",
    )
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
    unrealized_pnl = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text=(
            "Conservative net liquidation P&L while open; retained as net realized "
            "P&L after close for backward compatibility with analytics/risk callers."
        ),
    )
    last_mark_price = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True,
        help_text="Latest pre-slippage reference price used for paper mark-to-market.",
    )
    exit_reference_price = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True,
        help_text="Pre-slippage market/trigger price that caused the paper exit.",
    )
    exit_price = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True,
        help_text="Executed exit fill price after paper slippage, or a live broker fill.",
    )
    gross_realized_pnl = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True,
        help_text="Closed-trade P&L at reference prices before slippage and fees.",
    )
    slippage_cost = models.DecimalField(
        max_digits=16, decimal_places=2, default=0,
        help_text="Round-trip paper slippage drag in currency units.",
    )
    fees = models.DecimalField(
        max_digits=16, decimal_places=2, default=0,
        help_text="Round-trip modeled brokerage, taxes, and exchange fees.",
    )
    total_costs = models.DecimalField(
        max_digits=16, decimal_places=2, default=0,
        help_text="slippage_cost + fees for the closed trade.",
    )
    realized_pnl = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True,
        help_text="Net closed-trade P&L after all modeled or broker-reported costs.",
    )
    paper_slippage_bps_per_side = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text="Paper slippage assumption snapshotted at open; null for live positions.",
    )
    paper_fees_bps_per_side = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text="Paper aggregate fee assumption snapshotted at open; null for live positions.",
    )
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "open_positions"
        ordering = ["-opened_at"]
        indexes = [
            models.Index(fields=["symbol"]),
            models.Index(fields=["closed_at"]),
            models.Index(
                fields=["execution_mode", "closed_at"],
                name="open_pos_mode_closed_idx",
            ),
            models.Index(
                fields=["execution_mode", "timeframe", "closed_at"],
                name="open_pos_mode_tf_closed_idx",
            ),
        ]

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def __str__(self):
        state = "OPEN" if self.is_open else "CLOSED"
        return f"{self.symbol} {self.side} x{self.qty} [{state}]"


class BrokerOrder(models.Model):
    """Durable journal for every live-broker submission.

    The row is created *before* the network call.  If the process dies after
    Angel One accepts an order but before the response is persisted, the
    idempotency tag remains available for order-book reconciliation and the
    signal remains ``executing`` rather than being submitted again.
    """

    class Purpose(models.TextChoices):
        OPEN = "open", "Open"
        CLOSE = "close", "Close"
        FLATTEN = "flatten", "Emergency flatten"

    class Status(models.TextChoices):
        CREATED = "created", "Created locally"
        SUBMITTED = "submitted", "Submitted"
        FILLED = "filled", "Filled"
        CANCELLED = "cancelled", "Cancelled"
        REJECTED = "rejected", "Rejected"
        UNKNOWN = "unknown", "Broker state unknown"

    idempotency_key = models.UUIDField(
        unique=True, default=uuid.uuid4, editable=False,
        help_text="Sent as Angel One's ordertag and used for reconciliation.",
    )
    signal = models.ForeignKey(
        "signals.TradingSignal", null=True, blank=True,
        on_delete=models.PROTECT, related_name="broker_orders",
    )
    position = models.ForeignKey(
        OpenPosition, null=True, blank=True,
        on_delete=models.PROTECT, related_name="broker_orders",
    )
    purpose = models.CharField(max_length=12, choices=Purpose.choices)
    symbol = models.CharField(max_length=64, db_index=True)
    side = models.CharField(max_length=4, choices=[("BUY", "Buy"), ("SELL", "Sell")])
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CREATED, db_index=True)
    broker_order_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    filled_quantity = models.PositiveIntegerField(null=True, blank=True)
    average_fill_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "broker_orders"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="broker_ord_status_62f37e_idx"),
            models.Index(fields=["signal", "purpose"], name="broker_ord_signal_16d16f_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["signal", "purpose"], name="uniq_broker_signal_purpose"),
        ]

    def __str__(self):
        return f"{self.purpose} {self.side} {self.symbol} x{self.quantity} [{self.status}]"
