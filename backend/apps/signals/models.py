from django.db import models

from common.constants import MarketRegime, SignalStatus, SignalType


class TradingSignal(models.Model):
    """
    manual section 7: trading_signals. This is the central "why did/didn't
    a trade happen" record the manual's Final Rule (section 22) depends
    on -- every signal is stored whether or not it was ever executed, and
    `reason` always explains rejections in plain text so the daily review
    (apps/learning) has something concrete to read.

    The four score fields (technical_score, sentiment_score, risk_score,
    options_score) are stored individually rather than only storing
    `total_score` so that later analysis can answer "was this trade
    rejected because of a weak technical setup, bad sentiment, options
    flow disagreeing, or a risk-engine veto?" without needing to
    recompute anything.
    """

    symbol = models.CharField(max_length=32, db_index=True)
    signal_type = models.CharField(max_length=16, choices=SignalType.choices)
    entry_price = models.DecimalField(max_digits=14, decimal_places=4)
    stop_loss = models.DecimalField(max_digits=14, decimal_places=4)
    target_1 = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    target_2 = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    position_size = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)

    class OptionSide(models.TextChoices):
        CALL = "CE", "Call"
        PUT = "PE", "Put"

    # Only set by apps.options.index_direction_strategy's APPROVED
    # signals -- the underlying/index engine (apps.signals.engine)
    # trades the symbol itself, not an option contract, so these stay
    # null for every signal it produces. Inlined as their own choices
    # class here (rather than importing apps.options.models.
    # OptionContract.OptionType) to avoid apps.signals.models depending
    # on apps.options.models at Django app-registry load time -- the
    # two enums' values ("CE"/"PE") are already the shared vocabulary
    # apps.options.models.OptionContract itself uses.
    option_side = models.CharField(
        max_length=2, choices=OptionSide.choices, null=True, blank=True,
        help_text=(
            "CE or PE -- which option side this signal's suggested contract "
            "is on (apps.options.strike_selector.suggest_best_strike), "
            "distinct from signal_type (BUY/SELL), which reflects how the "
            "underlying position that actually executes is opened."
        ),
    )
    strike_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="The suggested option contract's strike, when option_side is set.",
    )
    option_contract = models.ForeignKey(
        "options.OptionContract", null=True, blank=True,
        on_delete=models.PROTECT, related_name="signals",
        help_text=(
            "Set only by apps.options.index_direction_strategy when suggest_best_strike "
            "resolved an actual OptionContract row for this signal -- this (not "
            "option_side/strike_price, which are display-only) is what "
            "apps.execution.paper_executor keys off to open a real option-premium "
            "position instead of the underlying-index proxy. entry_price/stop_loss/"
            "target_1/target_2 are in OPTION PREMIUM terms whenever this is set, not "
            "index points -- see index_direction_strategy's own docstring."
        ),
    )

    total_score = models.FloatField()
    technical_score = models.FloatField()
    sentiment_score = models.FloatField()
    risk_score = models.FloatField()
    options_score = models.FloatField(
        default=0.5,
        help_text="Options-chain confirmation score from "
                  "apps.options.signals_engine.options_confluence_score() -- "
                  "0.5 (neutral) when no options data is synced for the symbol yet.",
    )
    regime = models.CharField(max_length=32, choices=MarketRegime.choices)

    ml_win_probability = models.FloatField(
        null=True, blank=True,
        help_text=(
            "Win-probability from apps.learning.ml_train's logistic-regression "
            "model (see that module's docstring), scored at signal-creation "
            "time via apps.learning.ml_predict. Purely advisory -- for the "
            "dashboard and daily review to read alongside the four rule-based "
            "scores above. NULL until a model has been trained (needs "
            "apps.learning.ml_train.MIN_TRAINING_SAMPLES closed trades) and "
            "never gates BUY/NO_TRADE on its own; the rule engine above this "
            "field is still the only thing that decides approved/rejected."
        ),
    )

    # Raw indicator snapshot at signal-creation time (apps.market_data.
    # indicators.compute_indicators' `ind` dict), stored here -- not
    # recomputed later -- specifically so apps.learning.ml_features can
    # feed the win-probability model richer inputs than the four
    # composite scores alone, without training and live inference ever
    # being able to compute this snapshot two different ways (the exact
    # risk _create_signal's docstring already flagged when composite
    # scores were the only features). All nullable: legacy rows created
    # before this field existed, and the no-historical-data NO_TRADE
    # branch (which never reaches compute_indicators' return), leave
    # these unset -- ml_features treats a null the same as "feature not
    # available for this row," never as a crash.
    #
    # Stored pre-normalized (ratios, not raw price-scale numbers) so a
    # feature column means the same thing across symbols at very
    # different price levels (NIFTY vs. a low-priced stock): *_pct
    # fields divide the raw indicator by that candle's close price.
    # rsi/adx are already 0-100 scales and bb_width/relative_volume are
    # already unitless ratios, so those are stored as compute_indicators
    # returns them.
    ind_rsi = models.FloatField(null=True, blank=True, help_text="RSI (0-100) at signal time.")
    ind_adx = models.FloatField(null=True, blank=True, help_text="ADX (0-100) at signal time.")
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


    class RejectionStage(models.TextChoices):
        DIRECTION = "direction", "Underlying Direction"
        DATA_QUALITY = "data_quality", "Data Quality"
        SUCCESS_RATE = "success_rate", "Historical Success Rate"
        SENTIMENT = "sentiment", "Sentiment Veto"
        OPTIONS_CONFLUENCE = "options_confluence", "Options-Chain Confluence"
        EXPIRY = "expiry", "Expiry Selection"
        STRIKE = "strike", "Strike Selection"
        LIQUIDITY = "liquidity", "Liquidity Validation"
        RISK = "risk", "Risk Validation"
        ML_CONFIDENCE = "ml_confidence", "ML Confidence"
        LOT_SIZE = "lot_size", "Lot-Size Rounding"

    status = models.CharField(max_length=16, choices=SignalStatus.choices, default=SignalStatus.PENDING)
    execution_mode = models.CharField(
        max_length=8,
        choices=[("paper", "Paper"), ("live", "Live")],
        default="paper",
        db_index=True,
        help_text="Execution venue intended when this signal was generated.",
    )
    rejection_stage = models.CharField(
        max_length=24, choices=RejectionStage.choices, null=True, blank=True,
        help_text=(
            "Only set by apps.options.index_direction_strategy, on a REJECTED/NO_TRADE "
            "signal, to the first pipeline stage that added a reason to `reasons` -- "
            "lets the frontend render a real 'which stage did this die at' lifecycle "
            "waterfall instead of parsing the free-text `reason` string. Null for "
            "APPROVED signals and for every signal apps.signals.engine.generate_signal "
            "produces (that engine has no equivalent multi-stage pipeline to attribute "
            "a rejection to)."
        ),
    )
    reason = models.TextField(
        help_text="Human-readable explanation, especially for rejections -- "
                  "this is what the daily review reads."
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Links to the strategy/model version that produced this signal, so a
    # later rollback (manual section 14) can find every signal a bad
    # version produced. Nullable + PROTECT: a signal must never silently
    # lose its provenance, and a strategy version must never be deletable
    # while signals still reference it.
    strategy_version = models.ForeignKey(
        "learning.StrategyVersion", null=True, blank=True,
        on_delete=models.PROTECT, related_name="signals",
    )

    class Meta:
        db_table = "trading_signals"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["symbol", "-created_at"]), models.Index(fields=["status"])]

    def __str__(self):
        return f"{self.symbol} {self.signal_type} score={self.total_score:.2f} [{self.status}]"


class SignalFeatureSnapshot(models.Model):
    """
    The Feature Store / Signal Audit Trail: one row per TradingSignal,
    storing the full feature vector "as of" that signal's own creation
    moment (apps.options.feature_store.build_feature_vector's output --
    trend/momentum/OI/volume/skew/liquidity/expected-move/risk-reward
    scores, raw greeks, market regime, time-of-day, strike distance).

    Deliberately a snapshot, not a live-recomputable view: re-deriving
    these values later from current option-chain/candle data would
    silently answer a DIFFERENT question ("what does the market look
    like NOW") than what this table exists for ("what did the model
    actually see at decision time") -- the same reason apps.signals.
    models.TradingSignal's own ind_* fields are stored rather than
    recomputed, applied here to the full feature set apps.options'
    analytics engines produce, not just the four composite scores.

    OneToOne (not a plain FK) -- exactly one snapshot per signal, taken
    once, never updated after creation.

    Populated by a best-effort apps.signals.signals.py post_save
    receiver (same "never let this write's failure look like a
    signal-generation failure" discipline as that file's existing
    WS-broadcast receiver) -- a missing snapshot for an old/edge-case
    signal is a gap in the audit trail, never a reason to fail the
    signal itself.
    """

    signal = models.OneToOneField(TradingSignal, on_delete=models.CASCADE, related_name="feature_snapshot")
    features = models.JSONField(
        help_text="apps.options.feature_store.build_feature_vector's output -- see that function's own docstring for the exact field set.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "signal_feature_snapshots"

    def __str__(self):
        return f"features for signal #{self.signal_id}"
