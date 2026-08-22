"""
Autonomous AI paper-trading vertical -- a fully separate accounting/
execution/decision surface from apps.execution's OpenPosition/
AccountEquity (which serve BOTH paper and live real-money-shaped
trading via ExecutionModeSetting). This app never opens an
OpenPosition and never imports apps.market_data.broker_client (see
services/angel_one_guard.py) -- it reads market data and reuses
selected existing services (contract/expiry resolution, liquidity
checks, indicator math, the ModelRegistry champion/challenger
registry), but every table below is exclusive to this subsystem.

Money/price fields are Decimal throughout (never float), matching
apps.execution.models.OpenPosition's own convention: 14,4 for
per-unit prices, 16,2 for aggregated currency amounts.
"""

from __future__ import annotations

import uuid

from django.db import models

from common.constants import PositionSide, RiskEventSeverity


class Action(models.TextChoices):
    """The AI's entire restricted action space -- quantity is never an
    AI-chosen action, it is always exactly one current contract lot."""

    HOLD = "hold", "Hold"
    BUY_CE_ONE_LOT = "buy_ce_one_lot", "Buy CE (1 lot)"
    BUY_PE_ONE_LOT = "buy_pe_one_lot", "Buy PE (1 lot)"
    EXIT_POSITION = "exit_position", "Exit Position"
    KEEP_CURRENT_STOP = "keep_current_stop", "Keep Current Stop"
    TIGHTEN_STOP = "tighten_stop", "Tighten Stop"
    TRAIL_STOP = "trail_stop", "Trail Stop"
    SKIP_SIGNAL = "skip_signal", "Skip Signal"
    ENTER_COOLDOWN = "enter_cooldown", "Enter Cooldown"
    STOP_FOR_DAY = "stop_for_day", "Stop For Day"


class OutcomeClassification(models.TextChoices):
    """Statistical diagnostic labels (module docstring: NOT guaranteed
    causal truth), attached to closed trades and, for HOLD/SKIP
    decisions, to the hypothetical outcome walked forward from them."""

    CORRECT_DIRECTION_GOOD_ENTRY = "correct_direction_good_entry", "Correct Direction, Good Entry"
    CORRECT_DIRECTION_EARLY_ENTRY = "correct_direction_early_entry", "Correct Direction, Early Entry"
    CORRECT_DIRECTION_STOP_TOO_TIGHT = "correct_direction_stop_too_tight", "Correct Direction, Stop Too Tight"
    WRONG_DIRECTION = "wrong_direction", "Wrong Direction"
    EARLY_EXIT = "early_exit", "Early Exit"
    LATE_EXIT = "late_exit", "Late Exit"
    OPTION_DECAY_LOSS = "option_decay_loss", "Option Decay Loss"
    SPREAD_SLIPPAGE_LOSS = "spread_slippage_loss", "Spread/Slippage Loss"
    FALSE_BREAKOUT = "false_breakout", "False Breakout"
    OVERTRADING = "overtrading", "Overtrading"
    MARKET_REGIME_MISMATCH = "market_regime_mismatch", "Market Regime Mismatch"
    DATA_OR_EXECUTION_ERROR = "data_or_execution_error", "Data or Execution Error"


def get_or_create_account() -> "PaperAccount":
    """
    Seeds the singleton PaperAccount from settings.INITIAL_PAPER_CAPITAL
    exactly once. Never reset on restart or on a new trading day --
    equity is continuous across days (spec: "No automatic daily reset
    of the main account balance"); PaperDailySummary is where per-day
    snapshots live instead.
    """
    from django.conf import settings

    capital = settings.INITIAL_PAPER_CAPITAL
    account, _ = PaperAccount.objects.get_or_create(
        pk=1,
        defaults={
            "initial_capital": capital,
            "available_cash": capital,
            "current_equity": capital,
            "peak_equity": capital,
            "daily_start_equity": capital,
        },
    )
    return account


class PaperAccount(models.Model):
    """Singleton (pk=1, same enforced pattern as apps.execution.models.
    ExecutionModeSetting / apps.risk.models.KillSwitchState/AccountEquity)."""

    initial_capital = models.DecimalField(max_digits=16, decimal_places=2)
    available_cash = models.DecimalField(max_digits=16, decimal_places=2)
    reserved_capital = models.DecimalField(
        max_digits=16, decimal_places=2, default=0,
        help_text="Capital set aside for a pending (not-yet-filled) entry order.",
    )
    used_capital = models.DecimalField(
        max_digits=16, decimal_places=2, default=0,
        help_text="Capital tied up in the current open position's cost basis.",
    )
    realized_pnl = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    unrealized_pnl = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    gross_pnl = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_charges = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    net_pnl = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    current_equity = models.DecimalField(max_digits=16, decimal_places=2)
    peak_equity = models.DecimalField(max_digits=16, decimal_places=2)
    daily_start_equity = models.DecimalField(
        max_digits=16, decimal_places=2,
        help_text="Equity at the start of `trading_day` -- baseline for daily_drawdown.",
    )
    trading_day = models.DateField(null=True, blank=True)
    daily_drawdown = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    max_drawdown = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    currency = models.CharField(max_length=3, default="INR")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "paper_accounts"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"PaperAccount equity={self.current_equity}"


class PaperSessionState(models.Model):
    """Singleton FSM row driving apps.paper_trading.services.state_machine.
    Transition HISTORY is written via apps.admin_tools.audit.log_action
    (same pattern apps.execution.paper_executor already uses), not a
    dedicated table -- this row only ever holds the CURRENT state."""

    class State(models.TextChoices):
        FLAT = "flat", "Flat"
        ANALYZING = "analyzing", "Analyzing"
        ENTRY_CANDIDATE = "entry_candidate", "Entry Candidate"
        ORDER_PENDING = "order_pending", "Order Pending"
        POSITION_OPEN = "position_open", "Position Open"
        POSITION_MANAGEMENT = "position_management", "Position Management"
        EXIT_PENDING = "exit_pending", "Exit Pending"
        POSITION_CLOSED = "position_closed", "Position Closed"
        COOLDOWN = "cooldown", "Cooldown"
        FEED_STALE = "feed_stale", "Feed Stale"
        RISK_BLOCKED = "risk_blocked", "Risk Blocked"
        TRADING_STOPPED = "trading_stopped", "Trading Stopped"
        MARKET_CLOSED = "market_closed", "Market Closed"
        ERROR_SAFE_MODE = "error_safe_mode", "Error Safe Mode"

    state = models.CharField(max_length=24, choices=State.choices, default=State.FLAT)
    entered_at = models.DateTimeField(auto_now=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    context_json = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "paper_session_state"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return self.state


class PaperOrder(models.Model):
    class Side(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    class OrderType(models.TextChoices):
        MARKET = "market", "Market"
        LIMIT = "limit", "Limit"

    class Purpose(models.TextChoices):
        ENTRY = "entry", "Entry"
        EXIT = "exit", "Exit"

    class Status(models.TextChoices):
        NEW = "new", "New"
        VALIDATED = "validated", "Validated"
        ACCEPTED = "accepted", "Accepted"
        OPEN = "open", "Open"
        PARTIALLY_FILLED = "partially_filled", "Partially Filled"
        FILLED = "filled", "Filled"
        CANCELLED = "cancelled", "Cancelled"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    OPEN_STATUSES = (Status.NEW, Status.VALIDATED, Status.ACCEPTED, Status.OPEN, Status.PARTIALLY_FILLED)

    idempotency_key = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(PaperAccount, on_delete=models.PROTECT, related_name="orders")
    contract = models.ForeignKey("options.OptionContract", on_delete=models.PROTECT, related_name="paper_orders")
    purpose = models.CharField(max_length=8, choices=Purpose.choices)
    side = models.CharField(max_length=4, choices=Side.choices)
    order_type = models.CharField(max_length=8, choices=OrderType.choices, default=OrderType.MARKET)
    limit_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    quantity = models.PositiveIntegerField()
    lot_size_snapshot = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW, db_index=True)
    reject_reason = models.TextField(blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    filled_quantity = models.PositiveIntegerField(default=0)
    average_fill_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    fill_model_version = models.CharField(max_length=16, blank=True)
    pending_entry_marker = models.PositiveSmallIntegerField(
        null=True, blank=True, editable=False, default=None,
        help_text=(
            "MySQL does not support unique constraints WITH A CONDITION "
            "(confirmed: Django emits W036 and silently skips creating a "
            "conditional constraint on MySQL, unlike PostgreSQL) -- this is "
            "the standard workaround: set to 1 by "
            "PaperExecutionService.submit_order whenever purpose=ENTRY and "
            "status is still pending (NEW/VALIDATED/ACCEPTED/OPEN/"
            "PARTIALLY_FILLED), and reset to NULL the moment the order "
            "leaves that set (FILLED/CANCELLED/REJECTED/EXPIRED). MySQL (like "
            "every mainstream DB) treats multiple NULLs as distinct for "
            "unique-index purposes, so the UniqueConstraint below only ever "
            "actually constrains the (at most one) row per account where "
            "this is 1 -- enforcing 'only one pending entry order' at the "
            "DB level, not just in application code."
        ),
    )

    class Meta:
        db_table = "paper_orders"
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["account", "status"], name="paper_ord_acct_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "pending_entry_marker"],
                name="one_pending_paper_entry_order",
            ),
        ]

    def __str__(self):
        return f"{self.purpose} {self.side} {self.contract} x{self.quantity} [{self.status}]"


class PaperOrderEvent(models.Model):
    """Append-only. One row per PaperOrder status transition, written
    inside the same transaction as the transition itself."""

    order = models.ForeignKey(PaperOrder, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    detail_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "paper_order_events"
        ordering = ["created_at"]

    def __str__(self):
        return f"order={self.order_id} {self.from_status}->{self.to_status}"


class PaperPosition(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    account = models.ForeignKey(PaperAccount, on_delete=models.PROTECT, related_name="positions")
    contract = models.ForeignKey("options.OptionContract", on_delete=models.PROTECT, related_name="paper_positions")
    entry_order = models.ForeignKey(PaperOrder, on_delete=models.PROTECT, related_name="opened_positions")
    entry_decision_id = models.UUIDField(
        null=True, blank=True,
        help_text="The PaperAIDecision.decision_id that opened this position -- lets "
                  "position_service.close_position backfill that same decision row's "
                  "trade/gross_pnl/net_pnl/mfe/mae/net_r/outcome_classification once "
                  "the outcome is known, instead of creating a second decision record.",
    )
    side = models.CharField(max_length=8, choices=PositionSide.choices, default=PositionSide.LONG)
    quantity = models.PositiveIntegerField()
    average_entry_price = models.DecimalField(max_digits=14, decimal_places=4)
    initial_stop_loss = models.DecimalField(
        max_digits=14, decimal_places=4,
        help_text="Immutable snapshot of stop_loss at open -- never mutated, unlike stop_loss "
                  "itself (trailing/tightening moves that). Basis for initial_risk/R-multiple.",
    )
    stop_loss = models.DecimalField(max_digits=14, decimal_places=4)
    target_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    trailing_stop_distance = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    peak_favorable_price = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True,
        help_text="Highest mark price seen since entry -- MFE input.",
    )
    trough_adverse_price = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True,
        help_text="Lowest mark price seen since entry -- MAE input.",
    )
    last_mark_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    unrealized_pnl = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    open_marker = models.PositiveSmallIntegerField(
        null=True, blank=True, editable=False, default=1,
        help_text=(
            "Same MySQL partial-unique-index workaround as PaperOrder."
            "pending_entry_marker -- see that field's docstring. Set to 1 "
            "at creation (a PaperPosition is always created OPEN) and reset "
            "to NULL by PaperPositionService the moment status becomes "
            "CLOSED, so at most one row per account can ever have this set, "
            "enforcing 'only one open position at a time' at the DB level."
        ),
    )

    class Meta:
        db_table = "paper_positions"
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(fields=["account", "open_marker"], name="one_open_paper_position"),
        ]

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.OPEN

    def __str__(self):
        return f"{self.contract} x{self.quantity} [{self.status}]"


class PaperTrade(models.Model):
    """One closed round-trip. Created when a PaperPosition closes."""

    position = models.OneToOneField(PaperPosition, on_delete=models.PROTECT, related_name="trade")
    entry_order = models.ForeignKey(PaperOrder, on_delete=models.PROTECT, related_name="+")
    exit_order = models.ForeignKey(PaperOrder, on_delete=models.PROTECT, related_name="+")
    entry_price = models.DecimalField(max_digits=14, decimal_places=4)
    exit_price = models.DecimalField(max_digits=14, decimal_places=4)
    quantity = models.PositiveIntegerField()
    gross_pnl = models.DecimalField(max_digits=16, decimal_places=2)
    brokerage = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    stt = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    exchange_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sebi_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    stamp_duty = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    slippage_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_pnl = models.DecimalField(max_digits=16, decimal_places=2)
    charge_rule_version = models.CharField(max_length=32)
    fill_model_version = models.CharField(max_length=16)
    closed_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "paper_trades"
        ordering = ["-closed_at"]

    def __str__(self):
        return f"trade position={self.position_id} net_pnl={self.net_pnl}"


class PaperTradeResult(models.Model):
    """Diagnostic extension of PaperTrade -- MFE/MAE/R-multiple/outcome
    attribution, kept separate from the core financial record per the
    plan's accounting-vs-diagnostics split."""

    trade = models.OneToOneField(PaperTrade, on_delete=models.CASCADE, related_name="result")
    initial_stop_distance = models.DecimalField(max_digits=14, decimal_places=4)
    stop_modifications_json = models.JSONField(
        default=list, blank=True,
        help_text="List of {timestamp, old, new, reason} -- every stop change during the trade.",
    )
    mfe = models.DecimalField(max_digits=14, decimal_places=4, help_text="Maximum Favorable Excursion, in premium points.")
    mae = models.DecimalField(max_digits=14, decimal_places=4, help_text="Maximum Adverse Excursion, in premium points.")
    r_multiple = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    time_in_trade_seconds = models.PositiveIntegerField()
    best_unrealized_profit = models.DecimalField(max_digits=16, decimal_places=2)
    worst_unrealized_loss = models.DecimalField(max_digits=16, decimal_places=2)
    exit_reason = models.CharField(max_length=64)
    price_movement_after_exit = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    direction_correct = models.BooleanField(null=True)
    stop_too_tight = models.BooleanField(default=False)
    exit_early = models.BooleanField(default=False)
    exit_late = models.BooleanField(default=False)
    outcome_classification = models.CharField(max_length=40, choices=OutcomeClassification.choices, blank=True)

    class Meta:
        db_table = "paper_trade_results"

    def __str__(self):
        return f"result trade={self.trade_id} r={self.r_multiple}"


class PaperLedger(models.Model):
    """Append-only financial ledger. Never updated in place -- a
    correction is a new offsetting ADJUSTMENT row, not an edit."""

    class EntryType(models.TextChoices):
        CAPITAL_RESERVE = "capital_reserve", "Capital Reserve"
        CAPITAL_RELEASE = "capital_release", "Capital Release"
        REALIZED_PNL = "realized_pnl", "Realized P&L"
        CHARGE = "charge", "Charge"
        ADJUSTMENT = "adjustment", "Adjustment"

    account = models.ForeignKey(PaperAccount, on_delete=models.PROTECT, related_name="ledger_entries")
    entry_type = models.CharField(max_length=20, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=16, decimal_places=2, help_text="Signed -- positive credits available_cash, negative debits it.")
    balance_after = models.DecimalField(max_digits=16, decimal_places=2)
    reference_type = models.CharField(max_length=32, blank=True)
    reference_id = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "paper_ledger"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.entry_type} {self.amount} -> {self.balance_after}"


class PaperChargeRuleSet(models.Model):
    """
    Versioned, effective-date-based charge rules (spec: "do not scatter
    hardcoded charge rates throughout the code"). PaperPnLService always
    looks up `objects.filter(effective_from__lte=trade_date).order_by(
    "-effective_from").first()` -- the latest rule set effective on or
    before the trade's own date, so a rate change never retroactively
    alters an already-closed historical trade's stored charge figures
    (those stay whatever was actually charged at settlement time; only
    NEW trades pick up a newly added rule set).

    Rates are expressed as would-be-real NSE F&O charge components, in
    percent/bps of turnover unless noted -- update by adding a NEW row
    with a later effective_from, never by editing an existing row.
    """

    version = models.CharField(max_length=32, unique=True)
    effective_from = models.DateField(db_index=True)
    brokerage_flat_per_order = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    stt_pct_of_premium = models.DecimalField(max_digits=6, decimal_places=4, default=0, help_text="Securities Transaction Tax, sell side only, on options premium.")
    exchange_txn_pct = models.DecimalField(max_digits=6, decimal_places=4, default=0)
    gst_pct_of_brokerage_and_txn = models.DecimalField(max_digits=6, decimal_places=4, default=0)
    sebi_charges_pct = models.DecimalField(max_digits=8, decimal_places=6, default=0)
    stamp_duty_pct_buy_side = models.DecimalField(max_digits=6, decimal_places=4, default=0)
    other_charges_flat_per_order = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "paper_charge_rule_sets"
        ordering = ["-effective_from"]

    def __str__(self):
        return f"{self.version} (from {self.effective_from})"


class PaperRiskEvent(models.Model):
    """Mirrors apps.risk.models.RiskEvent's shape, paper-scoped. Includes
    the fail-closed trip wire: event_type="blocked_live_order_attempt"."""

    event_type = models.CharField(max_length=64, db_index=True)
    message = models.TextField()
    severity = models.CharField(max_length=8, choices=RiskEventSeverity.choices, default=RiskEventSeverity.INFO)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "paper_risk_events"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} [{self.severity}]"


class PaperDailySummary(models.Model):
    trading_day = models.DateField(unique=True)
    opening_equity = models.DecimalField(max_digits=16, decimal_places=2)
    closing_equity = models.DecimalField(max_digits=16, decimal_places=2)
    trades_count = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    gross_pnl = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_charges = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    net_pnl = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    win_rate = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    profit_factor = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    expectancy_r = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    max_drawdown = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    consecutive_losses = models.PositiveIntegerField(default=0)
    avg_holding_seconds = models.PositiveIntegerField(null=True, blank=True)
    avg_mfe = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    avg_mae = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    champion_model_version = models.CharField(max_length=50, blank=True)
    challenger_result_json = models.JSONField(default=dict, blank=True)
    promotion_status = models.CharField(max_length=32, blank=True)
    report_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "paper_daily_summaries"
        ordering = ["-trading_day"]

    def __str__(self):
        return f"{self.trading_day} net_pnl={self.net_pnl}"


class PaperAIDecision(models.Model):
    """Every AI decision -- traded, rejected, HOLD, SKIP, candidate not
    traded, re-entry, cooldown. source_environment=PAPER always, for
    now (spec: never read/train from a future LIVE table without
    explicit authorization)."""

    class SourceEnvironment(models.TextChoices):
        PAPER = "PAPER", "Paper"
        LIVE = "LIVE", "Live"

    decision_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(db_index=True)
    symbol = models.CharField(max_length=32, db_index=True)
    contract = models.ForeignKey(
        "options.OptionContract", null=True, blank=True, on_delete=models.PROTECT, related_name="paper_ai_decisions",
    )
    feature_snapshot_json = models.JSONField(default=dict)
    feature_schema_version = models.CharField(max_length=16)
    model_version = models.CharField(max_length=50, blank=True, help_text="Empty when the heuristic fallback policy decided (no active champion yet).")
    action = models.CharField(max_length=20, choices=Action.choices)
    confidence = models.FloatField(null=True, blank=True)
    expected_return = models.FloatField(null=True, blank=True)
    selected_stop = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    selected_exit_horizon = models.CharField(max_length=32, blank=True)
    risk_engine_response_json = models.JSONField(default=dict, blank=True)
    trade = models.ForeignKey(PaperTrade, null=True, blank=True, on_delete=models.PROTECT, related_name="decisions")
    is_hypothetical = models.BooleanField(
        default=True,
        help_text="True for HOLD/SKIP/rejected candidates that never opened a real PaperOrder.",
    )
    hypothetical_outcome_json = models.JSONField(
        null=True, blank=True,
        help_text="For is_hypothetical=True rows: the later outcome computed by walking candles "
                   "forward from `timestamp` without ever creating a PaperOrder -- see "
                   "services/outcome_service.py.",
    )
    gross_pnl = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    net_pnl = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    mfe = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    mae = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    net_r = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text="net_pnl_after_charges / initial_risk -- the normalized training label, never raw rupee P&L.",
    )
    outcome_classification = models.CharField(max_length=40, choices=OutcomeClassification.choices, blank=True)
    explanation_json = models.JSONField(
        default=dict, blank=True,
        help_text="Model-native feature importance/coefficients (or heuristic rule trace) -- "
                   "see services/explainability.py. Not a claim of proven causality.",
    )
    source_environment = models.CharField(max_length=8, choices=SourceEnvironment.choices, default=SourceEnvironment.PAPER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "paper_ai_decisions"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["source_environment", "is_hypothetical"], name="paper_dec_env_hyp_idx"),
            models.Index(fields=["symbol", "-timestamp"], name="paper_dec_symbol_ts_idx"),
        ]

    def __str__(self):
        return f"{self.action} {self.symbol} @ {self.timestamp}"
