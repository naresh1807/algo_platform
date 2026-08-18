from django.db import models


class StrategyVersion(models.Model):
    """
    manual section 7: strategy_versions. `params_json` holds ONLY the
    soft/tunable parameters the daily learning loop is allowed to touch
    (e.g. RSI threshold, sentiment weight) -- the hard risk limits in
    settings.RISK_HARD_LIMITS are never part of this JSON and can never
    be edited by an automated process (manual section 21: "Never
    auto-change core safety rules").

    Only one version should have active_flag=True at a time; enforcing
    that invariant belongs in a service function (apps/learning/services.py,
    not yet written), not in the model, so it can be wrapped in a
    transaction alongside the audit logging for the swap.

    Convention: when a version is promoted to active, record its
    expected performance under params_json["baseline_metrics"] (e.g.
    {"win_rate": 0.55}) -- apps.learning.tasks.check_for_drift compares
    live performance against this. There's no dedicated field for it
    (rather than a JSON sub-key) because "which metrics count as the
    baseline" may itself evolve without needing a migration.
    """

    version_name = models.CharField(max_length=100, unique=True)
    params_json = models.JSONField(default=dict)
    active_flag = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "strategy_versions"
        ordering = ["-created_at"]

    def __str__(self):
        marker = "ACTIVE" if self.active_flag else ""
        return f"{self.version_name} {marker}".strip()


class ModelRegistry(models.Model):
    """
    manual section 7 & 14: model_registry. `artifact_path` points at the
    serialized model file (kept out of the DB / git, e.g. on disk or
    object storage) -- only its path and metrics live here, so the
    registry stays a lightweight, queryable index rather than a blob
    store.
    """

    model_name = models.CharField(max_length=100)
    model_version = models.CharField(max_length=50)
    artifact_path = models.CharField(max_length=500)
    metrics_json = models.JSONField(default=dict)
    active_flag = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "model_registry"
        unique_together = ("model_name", "model_version")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.model_name} v{self.model_version}"


class DailyReviewNote(models.Model):
    """
    manual section 7 & 15: daily_review_notes. `approved_flag` is the
    human-in-the-loop gate: `suggested_changes` is written by the
    automated daily-review job (apps/learning/tasks.py:run_daily_review),
    but nothing in suggested_changes is applied to a StrategyVersion
    until a human sets approved_flag=True -- this is the "governance /
    approval workflow" the manual's AI Deployment Backbone requires.
    """

    review_date = models.DateField(unique=True)
    summary = models.TextField()
    suggested_changes = models.JSONField(default=dict)
    approved_flag = models.BooleanField(default=False)

    class Meta:
        db_table = "daily_review_notes"
        ordering = ["-review_date"]

    def __str__(self):
        return f"Review {self.review_date} ({'approved' if self.approved_flag else 'pending'})"


class DriftEvent(models.Model):
    """
    manual section 7 & 14: drift_events. severity here mirrors
    RiskEvent.severity conceptually but is deliberately a separate table
    (not reusing apps.risk.RiskEvent) -- drift is a MODEL/DATA quality
    signal, not a trading-risk signal, and the learning app should not
    depend on the risk app (keeps the dependency graph one-directional:
    risk can depend on learning's active StrategyVersion, but learning
    must never depend on risk).
    """

    detected_at = models.DateTimeField(auto_now_add=True)
    drift_type = models.CharField(max_length=64, help_text="e.g. data_drift, prediction_drift")
    metric_name = models.CharField(max_length=100)
    severity = models.CharField(max_length=16)
    action_taken = models.CharField(
        max_length=64, blank=True,
        help_text="e.g. rollback, quarantine, none",
    )

    class Meta:
        db_table = "drift_events"
        ordering = ["-detected_at"]

    def __str__(self):
        return f"{self.drift_type}/{self.metric_name} [{self.severity}]"


class TradeReview(models.Model):
    """
    manual 13.8 "Experience Memory" / 11.10-11.11 "Reward Engine" +
    "AI Learning Dataset": the per-closed-trade record of what the
    reward engine, confidence bands, and mistake-analysis heuristics
    concluded (see apps/learning/reward.py, confidence.py, mistakes.py).

    Deliberately its own table rather than extra columns on
    apps.execution.OpenPosition -- OpenPosition is apps.execution's own
    model (the paper/live executors' write path), and this is
    apps.learning's read of that outcome after the fact; keeping them
    separate matches the same one-directional dependency rule already
    documented on DriftEvent above (learning depends on execution's
    OpenPosition, never the reverse).

    Created once per position by apps/learning/signals.py's post_save
    hook on OpenPosition close -- not recomputed on every read -- so a
    later change to the reward/mistake heuristics doesn't retroactively
    rewrite history for trades already reviewed (same "don't recompute
    a stored snapshot" principle TradingSignal's ind_* fields already
    use).
    """

    position = models.OneToOneField(
        "execution.OpenPosition", on_delete=models.CASCADE, related_name="review",
    )
    r_multiple = models.FloatField(
        null=True, blank=True,
        help_text="P&L divided by planned risk (qty x |entry - stop|). Null if risk was zero/unknown.",
    )
    reward_score = models.IntegerField(
        null=True, blank=True,
        help_text="manual 11.10/13.9 discrete reward band -- see apps.learning.reward.compute_reward_score.",
    )
    confidence_band = models.CharField(
        max_length=32, default="Unrated",
        help_text="manual 13.10 label for the signal's ml_win_probability at entry time.",
    )
    mistake_tags = models.JSONField(
        default=list,
        help_text="manual 11.13/13.14 categories this trade matched -- see apps.learning.mistakes.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "trade_reviews"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Review of {self.position_id}: reward={self.reward_score} tags={self.mistake_tags}"


class HypotheticalTrade(models.Model):
    """
    A simulated (never-real-money) paper trade from one of
    apps.learning.strategy_methods' comparison methods -- the 5m swing
    group (trend-following, mean-reversion, breakout) or the 1m
    scalping group (ema-momentum, rsi-extreme, sar-volume-burst) --
    run continuously alongside the real strategy so their outcomes can
    be compared and the best one surfaced via ModelRegistry (see
    apps.learning.tasks.evaluate_strategy_methods).

    DELIBERATELY NOT apps.execution.OpenPosition, and has NO FK to it
    or to TradingSignal: OpenPosition shares a single AccountEquity/
    drawdown/consecutive-loss/kill-switch state and a "one open
    position per symbol" risk rule (apps.risk.engine) built for exactly
    ONE strategy running -- three more strategies opening positions
    into that same book would corrupt its real risk counters. Worse,
    apps/learning/signals.py's post_save hook on OpenPosition
    auto-creates a TradeReview for ANY closed position, which
    apps.learning.ml_train's win-probability trainer then reads
    globally with no strategy filter -- so hypothetical trades would
    silently leak into the real strategy's ML training data too. This
    table is fully isolated from all of that: apps.learning.tasks.
    run_strategy_method_comparison (the only writer) never imports
    paper_executor, apps.risk, OpenPosition, or TradingSignal, so there
    is no code path from this model to a broker call or to the real
    strategy's own state, live or paper.

    qty is always 1 (a fixed notional lot) -- comparison here is about
    price-path quality (win rate, R-multiple), not real position
    sizing, which depends on account equity these hypothetical trades
    deliberately never touch.
    """

    METHOD_CHOICES = [
        ("trend_following", "Trend Following"),
        ("mean_reversion", "Mean Reversion"),
        ("breakout", "Breakout"),
        # Scalping group (apps.learning.strategy_methods.SCALPING_METHOD_FUNCS,
        # 1m timeframe) -- same table, same isolation guarantees as the
        # three above, just a faster/tighter style of idea.
        ("ema_momentum_scalp", "EMA Momentum Scalp"),
        ("rsi_extreme_scalp", "RSI Extreme Reversal Scalp"),
        ("sar_volume_burst_scalp", "SAR + Volume Burst Scalp"),
    ]
    EXIT_REASON_CHOICES = [
        ("stop", "Stop Loss"),
        ("target", "Target"),
        ("timeout", "Max Holding Period"),
    ]

    class OptionSide(models.TextChoices):
        CALL = "CE", "Call"
        PUT = "PE", "Put"

    method = models.CharField(max_length=32, choices=METHOD_CHOICES, db_index=True)
    symbol = models.CharField(max_length=32, db_index=True)
    timeframe = models.CharField(max_length=8, default="5m")
    entry_price = models.DecimalField(max_digits=14, decimal_places=4)
    stop_loss = models.DecimalField(max_digits=14, decimal_places=4)
    target_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    qty = models.PositiveIntegerField(default=1)
    # Advisory only -- this trade's own win/loss simulation is always
    # against the underlying's price (entry/stop/target above), never
    # against option premium. Set once at open time (apps.learning.
    # tasks._run_comparison_cycle, via apps.options.strike_selector.
    # suggest_best_strike) purely so a trader glancing at this page has
    # a concrete "if you wanted to actually trade this idea with
    # options, here's the strike/side" answer instead of just an
    # underlying price level. Inlined as its own choices class (not
    # importing apps.options.models.OptionContract.OptionType) for the
    # same app-load-order reason apps.signals.models.TradingSignal.
    # OptionSide already documents. Null when no options data was
    # synced for this underlying/expiry at open time -- "not available"
    # is a normal, expected state, not an error.
    option_side = models.CharField(max_length=2, choices=OptionSide.choices, null=True, blank=True)
    strike_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Raw indicator snapshot at open time -- same fields, same docstring,
    # same pre-normalization convention (*_pct fields divided by that
    # candle's close) as apps.signals.models.TradingSignal's own ind_*
    # columns, kept in sync deliberately so apps.learning.scalp_ml_features
    # can reuse apps.learning.ml_features.RAW_INDICATOR_FEATURES verbatim
    # instead of a second, easily-drifting copy of the same list. Only
    # ever set for the SCALPING_METHOD_FUNCS group (apps.learning.tasks.
    # _run_comparison_cycle) -- the three swing METHOD_FUNCS ideas don't
    # populate these, since apps.learning.scalp_ml_train's model is scoped
    # to scalping trades only (see that module's own docstring).
    ind_rsi = models.FloatField(null=True, blank=True, help_text="RSI (0-100) at open time.")
    ind_adx = models.FloatField(null=True, blank=True, help_text="ADX (0-100) at open time.")
    ind_bb_width = models.FloatField(
        null=True, blank=True,
        help_text="Bollinger band width as a fraction of the middle band (already unitless).",
    )
    ind_relative_volume = models.FloatField(
        null=True, blank=True, help_text="Volume vs. its 20-period average, as a ratio.",
    )
    ind_atr_pct = models.FloatField(
        null=True, blank=True, help_text="ATR divided by close price (scale-free volatility).",
    )
    ind_macd_hist_pct = models.FloatField(
        null=True, blank=True, help_text="MACD histogram divided by close price.",
    )
    ind_ema9_slope_pct = models.FloatField(
        null=True, blank=True, help_text="EMA9 candle-over-candle change divided by close price.",
    )
    ind_ema21_slope_pct = models.FloatField(
        null=True, blank=True, help_text="EMA21 candle-over-candle change divided by close price.",
    )
    ml_win_probability = models.FloatField(
        null=True, blank=True,
        help_text=(
            "Win-probability from apps.learning.scalp_ml_train's own logistic-regression "
            "model (trained ONLY on closed scalping HypotheticalTrade rows -- a separate "
            "model from apps.signals.models.TradingSignal.ml_win_probability's, see "
            "apps.learning.scalp_ml_train's module docstring for why they're kept apart), "
            "scored at open time via apps.learning.scalp_ml_predict. Purely advisory, same "
            "as the real model's field -- never gates which ideas open, so the daily "
            "method-comparison ranking keeps measuring each method's own unfiltered "
            "performance. NULL until a model has been trained (needs "
            "apps.learning.scalp_ml_train.MIN_TRAINING_SAMPLES closed scalps)."
        ),
    )

    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    exit_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    exit_reason = models.CharField(max_length=16, choices=EXIT_REASON_CHOICES, blank=True)
    pnl = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    r_multiple = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "hypothetical_trades"
        ordering = ["-opened_at"]
        indexes = [models.Index(fields=["method", "-opened_at"])]

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def __str__(self):
        state = "open" if self.is_open else f"closed pnl={self.pnl}"
        return f"[{self.method}] {self.symbol} ({state})"
