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
    tradingsymbol = models.CharField(
        max_length=64, default="",
        help_text=(
            "Broker's tradingsymbol for this contract (e.g. 'NIFTY28AUG25024500CE') -- "
            "required (alongside symbol_token) to actually place an order on this exact "
            "contract, as opposed to just fetching its quote."
        ),
    )
    lot_size = models.PositiveIntegerField(
        default=0,
        help_text="Contract's minimum tradeable quantity -- real orders must be a whole multiple of this.",
    )
    is_active = models.BooleanField(
        default=True, db_index=True,
        help_text=(
            "Whether this contract is currently eligible to be traded/quoted -- maintained by "
            "apps.options.contract_sync using apps.options.expiry_service.is_expiry_eligible "
            "(cutoff-aware: a contract expiring TODAY stays active until "
            "settings.OPTIONS_EXPIRY_CUTOFF_TIME, not just until midnight). Never a reason to "
            "delete a row -- historical/expired contracts stay in the table (is_active=False) so "
            "past OptionChainSnapshot data remains attached for backtesting/analytics; live-trading "
            "code should filter on this instead of re-deriving expiry eligibility itself."
        ),
    )

    class Meta:
        db_table = "option_contracts"
        unique_together = ("underlying", "expiry", "strike", "option_type")
        ordering = ["underlying", "expiry", "strike"]
        indexes = [
            models.Index(fields=["underlying", "expiry"]),
            models.Index(fields=["underlying", "is_active"]),
        ]

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


class OptionsStrategySetting(models.Model):
    """
    Singleton (always exactly one row, same enforced-pk=1 pattern as
    apps.execution.models.ExecutionModeSetting) holding the operator-
    controlled expiry/strike selection preferences for
    apps.options.index_direction_strategy -- runtime-configurable via
    apps.options.views.OptionsStrategySettingView so a change made from
    the frontend takes effect on the very next scheduled evaluation, no
    process restart needed, same "DB-backed override, not a static
    settings.py constant" reasoning ExecutionModeSetting's own docstring
    gives for the paper/live toggle.

    Deliberately NOT where risk/liquidity thresholds live (those stay in
    settings.RISK_HARD_LIMITS, which is intentionally never DB-editable
    so the learning loop can never weaken a hard limit) -- this model is
    strategy preference (WHICH expiry/strike to prefer), not risk policy
    (whether a candidate is safe enough to trade).
    """

    class ExpiryMode(models.TextChoices):
        CURRENT_WEEK = "current_week", "Current Week"
        NEXT_WEEK = "next_week", "Next Week"
        MONTHLY = "monthly", "Monthly"
        CUSTOM = "custom", "Custom Expiry"

    class StrikeMode(models.TextChoices):
        ATM = "atm", "ATM"
        ITM = "itm", "ITM"
        OTM = "otm", "OTM"
        AI = "ai", "AI Selected"

    expiry_mode = models.CharField(max_length=16, choices=ExpiryMode.choices, default=ExpiryMode.CURRENT_WEEK)
    strike_mode = models.CharField(max_length=8, choices=StrikeMode.choices, default=StrikeMode.AI)
    itm_otm_steps = models.PositiveSmallIntegerField(
        default=1,
        help_text="How many strikes ITM/OTM to step from ATM when strike_mode is ITM or OTM.",
    )
    custom_expiry = models.DateField(
        null=True, blank=True,
        help_text="Only used when expiry_mode=CUSTOM -- must match an actually-synced OptionContract expiry.",
    )
    changed_at = models.DateTimeField(auto_now=True)
    changed_by = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = "options_strategy_setting"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)

    def __str__(self):
        return f"expiry={self.expiry_mode} strike={self.strike_mode}"


def get_options_strategy_settings() -> "OptionsStrategySetting":
    """
    The live, DB-backed effective strategy settings. Callers that need
    to know which expiry/strike mode is active (apps.options.
    signals_engine.select_expiry, apps.options.strike_selector.
    suggest_best_strike) call this rather than hard-coding CURRENT_WEEK/
    AI, so a Settings-page change takes effect on the next evaluation.
    """
    row, _ = OptionsStrategySetting.objects.get_or_create(pk=1)
    return row


class ScoringWeights(models.Model):
    """
    Singleton (same enforced-pk=1 pattern as OptionsStrategySetting/
    ExecutionModeSetting) holding the RUNTIME-CONFIGURABLE weights
    apps.options.signal_scoring.calculate_signal_score combines apps.
    options.confirmation's per-factor breakdown with -- per this
    platform's own instruction that fixed scoring weights "MUST NOT be
    treated as universal." Deliberately DB-backed, not a settings.py
    constant, so weights can be tuned/validated against historical
    performance (apps.analytics) without a redeploy, the same
    "operator-controlled override" reasoning ExecutionModeSetting's own
    docstring gives.

    Field set matches apps.options.confirmation.evaluate_multi_signal_
    confirmation's ACTUAL 9 factors (trend, momentum, oi, volume, skew,
    greeks, liquidity, expected_move, risk_reward) -- not a literal
    match to any illustrative example weight list, since this build's
    confirmation engine doesn't compute separate standalone
    "market_structure"/"oi_change"/"iv" factors distinct from what's
    already folded into trend/oi/skew above. Defaults sum to 1.0, but
    apps.options.signal_scoring.calculate_signal_score renormalizes
    over whatever weight is actually available on a given signal (some
    factors are routinely unavailable, e.g. skew before enough
    contracts are synced), so an operator changing weights doesn't need
    to manually keep them summing to exactly 1.0 either.
    """

    trend_weight = models.FloatField(default=0.18)
    momentum_weight = models.FloatField(default=0.12)
    oi_weight = models.FloatField(default=0.15)
    volume_weight = models.FloatField(default=0.07)
    skew_weight = models.FloatField(default=0.08)
    greeks_weight = models.FloatField(default=0.10)
    liquidity_weight = models.FloatField(default=0.13)
    expected_move_weight = models.FloatField(default=0.08)
    risk_reward_weight = models.FloatField(default=0.09)
    changed_at = models.DateTimeField(auto_now=True)
    changed_by = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = "options_scoring_weights"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)

    def as_dict(self) -> dict:
        return {
            "trend": self.trend_weight, "momentum": self.momentum_weight, "oi": self.oi_weight,
            "volume": self.volume_weight, "skew": self.skew_weight, "greeks": self.greeks_weight,
            "liquidity": self.liquidity_weight, "expected_move": self.expected_move_weight,
            "risk_reward": self.risk_reward_weight,
        }

    def __str__(self):
        return f"trend={self.trend_weight} oi={self.oi_weight} liquidity={self.liquidity_weight} ..."


class OptionSyncStatus(models.Model):
    """
    One row per underlying, maintained by apps.options.contract_sync on
    every sync attempt (success or failure) -- this is what makes the
    instrument-master/contract sync's health OBSERVABLE and survivable
    across process restarts: the in-process instrument-master cache
    (apps.options.instrument_master's own module-level dict) is real and
    useful for avoiding repeat downloads within one process's lifetime,
    but it can't answer "when did this last actually succeed" from a
    fresh Django shell, the API, or a Celery worker that just restarted
    -- that's what this table is for.

    Read by apps.options.expiry_service.expiry_status (the
    last_successful_sync the API/frontend surface) and by
    apps.options.tasks.options_sync_health_check (deciding whether a
    self-healing resync is needed) -- never written to by anything
    except apps.options.contract_sync.sync_underlying_contracts, so
    there is exactly one place that can make this table say something
    that didn't actually happen.
    """

    underlying = models.CharField(max_length=32, unique=True, db_index=True)
    last_successful_sync = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    last_result = models.JSONField(
        null=True, blank=True,
        help_text="Most recent sync_underlying_contracts() summary: expiries synced, "
                  "inserted/updated/deactivated/skipped counts -- same shape the management "
                  "command prints and the health check reads.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "option_sync_status"

    def __str__(self):
        return f"{self.underlying}: last_successful_sync={self.last_successful_sync}"


def get_scoring_weights() -> "ScoringWeights":
    """Live, DB-backed weights -- see ScoringWeights' own docstring for why this stays DB-backed, not a settings.py constant."""
    row, _ = ScoringWeights.objects.get_or_create(pk=1)
    return row
