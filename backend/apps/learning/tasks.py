"""
Celery tasks for the daily learning loop (manual section 15) and drift
detection (manual section 14). Scheduled from config/celery.py's
beat_schedule.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from celery import shared_task
from django.db.models import Avg
from django.utils import timezone

logger = logging.getLogger(__name__)

# How many trailing calendar days (inclusive of the review date) the
# daily review's suggested_changes heuristics now look at, instead of
# just the single day being reviewed. Calendar days, not trading days,
# for simplicity -- a stray weekend/holiday inside the window just
# means slightly fewer trades counted, not wrong ones. Widening the
# sample this way (see run_daily_review's docstring) is the single
# biggest lever on how noisy a one-day heuristic is; 5 trading days is
# a reasonable first step without needing weeks of backlog before the
# loop says anything at all.
ROLLING_REVIEW_WINDOW_DAYS = 5

# Minimum closed trades / rejected signals *within the rolling window*
# before a heuristic will suggest anything -- deliberately higher than
# the old single-day thresholds (5) since this is now judging a wider
# sample and should demand more evidence before proposing a change.
MIN_ROLLING_TRADES_FOR_STOP_HEURISTIC = 15
MIN_ROLLING_REJECTED_FOR_THRESHOLD_HEURISTIC = 15


# Drift thresholds -- how much recent performance can degrade vs. the
# active strategy version's recorded baseline before it counts as
# drift. Two tiers (warning vs. critical) mirror RiskEvent's severity
# levels conceptually, even though DriftEvent is deliberately its own
# table (see the model's docstring for why it isn't just reusing
# RiskEvent).
WIN_RATE_WARNING_DROP = 0.10   # 10 percentage points below baseline
WIN_RATE_CRITICAL_DROP = 0.20  # 20 percentage points below baseline
MIN_TRADES_FOR_DRIFT_CHECK = 10  # don't judge drift off a tiny, noisy sample


@shared_task
def run_daily_review(review_date: str | None = None):
    """
    review_date: ISO date string (defaults to today, IST). Runs after
    market close (config/celery.py beat_schedule: 16:00 IST).

    What this does NOT try to do: decide with any sophistication what
    should change. The suggested_changes drafted here are simple,
    transparent, rule-of-thumb heuristics -- exactly the kind of thing
    a human reviewing this note should treat as a prompt to go look
    closer, not a recommendation to trust blindly. A more principled
    version of this (walk-forward validated parameter search, per
    manual section 19's backtesting standards) now exists as a
    separate, explicit tool -- apps.analytics.backtest / `python
    manage.py run_backtest` -- for actually picking these parameters
    against historical data; this daily task stays a lightweight,
    same-day heuristic nudge, not a replacement for that.

    Heuristics now look at a ROLLING_REVIEW_WINDOW_DAYS-day trailing
    window ending on review_date, not just the single day -- a single
    day's win rate or rejection count is a small, noisy sample (a
    handful of trades can swing win rate by 20+ points on nothing more
    than luck), and a heuristic that reacts to that noise every day
    would have suggested_changes flip-flopping constantly. The summary
    text still reports today's own numbers on their own (so a human
    reading it can see today specifically), but suggested_changes is
    judged against the rolling window, with a correspondingly higher
    minimum-sample bar (MIN_ROLLING_TRADES_FOR_STOP_HEURISTIC etc.)
    since it's now allowed to draw on more days of evidence before
    saying anything.

    Regardless of what it suggests, this only ever writes a
    DailyReviewNote with approved_flag=False. Nothing here touches
    StrategyVersion. Turning an approved note into an actual new
    strategy version is a separate, explicit step -- see
    apps/learning/management/commands/apply_review_changes.py.
    """
    from apps.analytics.services import compute_daily_performance
    from apps.execution.models import OpenPosition
    from apps.learning.models import DailyReviewNote, StrategyVersion
    from apps.signals.models import TradingSignal
    from common.constants import SignalStatus

    target_date = date.fromisoformat(review_date) if review_date else timezone.localdate()

    metrics = compute_daily_performance(target_date)
    active_version = StrategyVersion.objects.filter(active_flag=True).first()

    day_start, day_end = target_date, target_date + timedelta(days=1)
    rejected_today = TradingSignal.objects.filter(
        created_at__gte=day_start, created_at__lt=day_end, status=SignalStatus.REJECTED,
    )
    rejected_count_today = rejected_today.count()
    avg_technical_score_rejected_today = (
        rejected_today.aggregate(avg=Avg("technical_score"))["avg"] or 0.0
    )

    # manual 13.11 "Self Evaluation" -- reward-score average and the
    # day's most common mistake tags, from the TradeReview rows
    # apps.learning.signals creates as each position closes (manual
    # 13.8 Experience Memory). Purely descriptive here; nothing in
    # this task acts on these numbers beyond reporting them, same as
    # every other figure in this summary.
    from apps.learning.models import TradeReview

    todays_reviews = TradeReview.objects.filter(
        position__closed_at__gte=day_start, position__closed_at__lt=day_end,
    )
    reward_scores = [r.reward_score for r in todays_reviews if r.reward_score is not None]
    avg_reward = sum(reward_scores) / len(reward_scores) if reward_scores else None

    mistake_counts: dict[str, int] = {}
    for review in todays_reviews:
        for tag in review.mistake_tags:
            mistake_counts[tag] = mistake_counts.get(tag, 0) + 1
    top_mistakes = sorted(mistake_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]

    summary_parts = [
        f"{metrics.total_trades} trade(s) closed today.",
    ]
    if metrics.win_rate is not None:
        summary_parts.append(f"Win rate {metrics.win_rate:.0%}.")
    if metrics.expectancy is not None:
        summary_parts.append(f"Expectancy {metrics.expectancy:+.2f}R per trade.")
    if avg_reward is not None:
        summary_parts.append(f"Average reward score {avg_reward:+.1f} ({len(reward_scores)} reviewed trade(s)).")
    if top_mistakes:
        summary_parts.append(
            "Most common mistake tags today: " + ", ".join(f"{tag} x{n}" for tag, n in top_mistakes) + "."
        )
    summary_parts.append(
        f"{rejected_count_today} signal(s) rejected today "
        f"(avg technical score {avg_technical_score_rejected_today:.0%})."
    )

    # --- Rolling window aggregates (what the heuristics below actually
    # judge against, per this function's docstring) ---
    window_start = target_date - timedelta(days=ROLLING_REVIEW_WINDOW_DAYS - 1)
    window_start_dt, window_end_dt = window_start, target_date + timedelta(days=1)

    rolling_closed = OpenPosition.objects.filter(
        closed_at__gte=window_start_dt, closed_at__lt=window_end_dt, signal__isnull=False,
    )
    rolling_trade_count = rolling_closed.count()
    rolling_wins = rolling_closed.filter(unrealized_pnl__gt=0).count() if rolling_trade_count else 0
    rolling_win_rate = (rolling_wins / rolling_trade_count) if rolling_trade_count else None

    rolling_rejected = TradingSignal.objects.filter(
        created_at__gte=window_start_dt, created_at__lt=window_end_dt, status=SignalStatus.REJECTED,
    )
    rolling_rejected_count = rolling_rejected.count()
    rolling_avg_technical_score_rejected = (
        rolling_rejected.aggregate(avg=Avg("technical_score"))["avg"] or 0.0
    )

    summary_parts.append(
        f"Trailing {ROLLING_REVIEW_WINDOW_DAYS}-day window ({window_start.isoformat()}\u2013"
        f"{target_date.isoformat()}): {rolling_trade_count} trade(s)"
        + (f", win rate {rolling_win_rate:.0%}" if rolling_win_rate is not None else "")
        + f", {rolling_rejected_count} rejected."
    )

    suggested_changes: dict = {}

    # Heuristic 1: if plenty of setups have been rejected over the
    # rolling window and their technical scores are clustering just
    # under the threshold, the threshold itself may be slightly too
    # strict for current market conditions -- suggest a small (not
    # drastic) loosening. Judged over the window, not just today, so
    # one unusually quiet or unusually busy day doesn't flip this
    # suggestion on its own.
    if (
        rolling_rejected_count >= MIN_ROLLING_REJECTED_FOR_THRESHOLD_HEURISTIC
        and rolling_avg_technical_score_rejected >= 0.55
    ):
        # Read the LIVE current value rather than hardcoding a stale
        # number here -- this constant has changed at least once
        # already (0.7 -> 0.8 on 2026-08-08 per a real backtest), and a
        # hardcoded string previously drifted out of sync with it,
        # silently misreporting the baseline to whoever reviews this
        # note. A small, fixed step down (not an absolute target) keeps
        # the suggestion meaningful regardless of what the current
        # value happens to be.
        from apps.signals.engine import TECHNICAL_SCORE_THRESHOLD as _current_threshold

        suggested_threshold = round(max(0.5, _current_threshold - 0.1), 2)
        suggested_changes["signals_engine.TECHNICAL_SCORE_THRESHOLD"] = {
            "current": f"{_current_threshold} (live value -- see apps/signals/engine.py)",
            "suggested": suggested_threshold,
            "why": (
                f"{rolling_rejected_count} signals rejected over the trailing "
                f"{ROLLING_REVIEW_WINDOW_DAYS} days with average technical score "
                f"{rolling_avg_technical_score_rejected:.0%}, close to the "
                f"{_current_threshold:.0%} threshold -- may be filtering out "
                f"marginally-acceptable setups. Consider validating against "
                f"apps.analytics.backtest / `python manage.py run_backtest` before applying."
            ),
        }

    # Heuristic 2: a poor win rate over the rolling window on a
    # reasonable sample size suggests the stop distance might be too
    # tight (getting stopped out on noise) rather than the entries
    # being wrong -- suggest reviewing the ATR stop multiplier rather
    # than jumping straight to "the strategy doesn't work". Also judged
    # over the window for the same noise-reduction reason as Heuristic 1.
    if (
        rolling_trade_count >= MIN_ROLLING_TRADES_FOR_STOP_HEURISTIC
        and rolling_win_rate is not None and rolling_win_rate < 0.35
    ):
        from apps.signals.engine import ATR_STOP_MULTIPLIER as _current_atr_multiplier

        suggested_changes["signals_engine.ATR_STOP_MULTIPLIER"] = {
            "current": f"{_current_atr_multiplier} (live value -- see apps/signals/engine.py)",
            "suggested": round(_current_atr_multiplier + 0.3, 2),
            "why": (
                f"Win rate {rolling_win_rate:.0%} over {rolling_trade_count} trades in the "
                f"trailing {ROLLING_REVIEW_WINDOW_DAYS} days -- consider whether stops "
                f"are too tight for current volatility before concluding the entry "
                f"logic itself is wrong. Consider validating against "
                f"apps.analytics.backtest / `python manage.py run_backtest` before applying."
            ),
        }

    if not suggested_changes:
        summary_parts.append("No parameter changes suggested (rolling-window heuristics).")

    note, _created = DailyReviewNote.objects.update_or_create(
        review_date=target_date,
        defaults={
            "summary": " ".join(summary_parts),
            "suggested_changes": suggested_changes,
            # Explicitly reset to unapproved even on a re-run -- if this
            # task is ever re-triggered for a date that was already
            # reviewed AND approved, re-running it should not silently
            # keep a stale approval on newly-recomputed suggestions.
            "approved_flag": False,
        },
    )
    return {"review_id": note.pk, "suggested_changes": suggested_changes}


@shared_task
def check_for_drift():
    """
    Compares the active StrategyVersion's recorded baseline win_rate
    (params_json["baseline_metrics"]["win_rate"], set when a version is
    promoted -- see StrategyVersion's docstring) against a recent
    rolling win_rate computed from closed OpenPosition rows.

    Per the manual's safety rule, this task may only ever *recommend* a
    rollback (by writing a DriftEvent with action_taken="recommended_rollback")
    -- it never flips StrategyVersion.active_flag itself. Promoting a
    different version in response is a separate, explicit human/admin
    action (via the StrategyVersion API/admin), same as approving a
    DailyReviewNote.
    """
    from apps.execution.models import OpenPosition
    from apps.learning.models import DriftEvent, StrategyVersion

    active_version = StrategyVersion.objects.filter(active_flag=True).first()
    if active_version is None:
        logger.info("check_for_drift: no active StrategyVersion set -- skipping.")
        return {"skipped": True, "reason": "no_active_strategy_version"}

    baseline = active_version.params_json.get("baseline_metrics", {})
    baseline_win_rate = baseline.get("win_rate")
    if baseline_win_rate is None:
        logger.info(
            "check_for_drift: active StrategyVersion %s has no baseline_metrics.win_rate "
            "recorded -- nothing to compare against yet.",
            active_version.version_name,
        )
        return {"skipped": True, "reason": "no_baseline_recorded"}

    recent_closed = OpenPosition.objects.filter(
        signal__strategy_version=active_version, closed_at__isnull=False,
    ).order_by("-closed_at")[:50]
    recent_closed = list(recent_closed)

    if len(recent_closed) < MIN_TRADES_FOR_DRIFT_CHECK:
        logger.info(
            "check_for_drift: only %d closed trades on this strategy version "
            "(need >= %d) -- sample too small to judge drift.",
            len(recent_closed), MIN_TRADES_FOR_DRIFT_CHECK,
        )
        return {"skipped": True, "reason": "insufficient_sample"}

    wins = sum(1 for p in recent_closed if p.unrealized_pnl > 0)
    recent_win_rate = wins / len(recent_closed)
    drop = baseline_win_rate - recent_win_rate

    if drop >= WIN_RATE_CRITICAL_DROP:
        severity, action = "critical", "recommended_rollback"
    elif drop >= WIN_RATE_WARNING_DROP:
        severity, action = "warning", "none"
    else:
        logger.info("check_for_drift: no significant drift (drop=%.3f).", drop)
        return {"drift_detected": False, "drop": drop}

    DriftEvent.objects.create(
        drift_type="prediction_drift",
        metric_name="win_rate",
        severity=severity,
        action_taken=action,
    )
    logger.warning(
        "Drift detected on %s: win_rate dropped %.1f%% (baseline %.1f%%, recent %.1f%% "
        "over %d trades). action=%s",
        active_version.version_name, drop * 100, baseline_win_rate * 100,
        recent_win_rate * 100, len(recent_closed), action,
    )
    return {
        "drift_detected": True, "severity": severity, "drop": drop,
        "recent_win_rate": recent_win_rate, "baseline_win_rate": baseline_win_rate,
    }


# Thresholds for apps.learning.tasks.monitor_ml_model_performance --
# deliberately separate constants from WIN_RATE_WARNING_DROP/
# WIN_RATE_CRITICAL_DROP above: those compare a STRATEGY's live win
# rate to its own baseline, this compares the ML MODEL's live
# prediction accuracy to what it scored on its own training-time
# holdout -- two different things drifting for two different reasons
# (market regime change vs. the model itself going stale).
ML_ACCURACY_WARNING_DROP = 0.15
ML_ACCURACY_CRITICAL_DROP = 0.30
MIN_TRADES_FOR_ML_MONITORING = 10


@shared_task
def monitor_ml_model_performance():
    """
    Daily production-monitoring check for the active win-probability
    model (config/celery.py beat_schedule): compares how often the
    model's own predictions (ml_win_probability >= 0.5 => "predicted
    win") actually matched real closed-trade outcomes, against the
    accuracy it reported on its own training-time holdout
    (ModelRegistry.metrics_json["accuracy"]). A live-vs-trained
    accuracy gap is the ML-layer equivalent of check_for_drift's
    win-rate comparison for a StrategyVersion -- same severity tiers,
    same DriftEvent table, but its own drift_type ("ml_model_drift")
    so the two causes (market regime shifted vs. this specific model
    going stale) stay distinguishable in the log.

    Same safety posture as check_for_drift: this only ever *logs* a
    DriftEvent (action_taken="recommended_retrain" at most) -- it never
    deactivates the model or forces an immediate retrain itself. The
    weekly retrain_win_probability_model task (and its own
    champion/challenger gate) is what actually replaces a degraded
    model, on its normal schedule or when a human reacts to this event.
    """
    from apps.execution.models import OpenPosition
    from apps.learning.models import DriftEvent, ModelRegistry

    active = ModelRegistry.objects.filter(model_name="win_probability", active_flag=True).first()
    if active is None:
        logger.info("monitor_ml_model_performance: no active win_probability model -- skipping.")
        return {"skipped": True, "reason": "no_active_model"}

    trained_accuracy = active.metrics_json.get("accuracy")
    if trained_accuracy is None:
        logger.info(
            "monitor_ml_model_performance: active model %s has no recorded accuracy "
            "to compare against -- skipping.", active,
        )
        return {"skipped": True, "reason": "no_recorded_accuracy"}

    recent_closed = list(
        OpenPosition.objects
        .filter(closed_at__isnull=False, signal__ml_win_probability__isnull=False)
        .select_related("signal")
        .order_by("-closed_at")[:50]
    )
    if len(recent_closed) < MIN_TRADES_FOR_ML_MONITORING:
        logger.info(
            "monitor_ml_model_performance: only %d ML-scored closed trades so far "
            "(need >= %d) -- sample too small to judge live accuracy.",
            len(recent_closed), MIN_TRADES_FOR_ML_MONITORING,
        )
        return {"skipped": True, "reason": "insufficient_sample"}

    correct = sum(
        1 for p in recent_closed
        if (p.signal.ml_win_probability >= 0.5) == (p.unrealized_pnl > 0)
    )
    live_accuracy = correct / len(recent_closed)
    drop = trained_accuracy - live_accuracy

    if drop >= ML_ACCURACY_CRITICAL_DROP:
        severity, action = "critical", "recommended_retrain"
    elif drop >= ML_ACCURACY_WARNING_DROP:
        severity, action = "warning", "none"
    else:
        logger.info("monitor_ml_model_performance: no significant drift (drop=%.3f).", drop)
        return {"drift_detected": False, "drop": drop, "live_accuracy": live_accuracy}

    DriftEvent.objects.create(
        drift_type="ml_model_drift",
        metric_name="win_probability_accuracy",
        severity=severity,
        action_taken=action,
    )
    logger.warning(
        "ML model drift on %s: live accuracy dropped %.1f%% (trained %.1f%%, live %.1f%% "
        "over %d trades). action=%s",
        active, drop * 100, trained_accuracy * 100, live_accuracy * 100,
        len(recent_closed), action,
    )
    return {
        "drift_detected": True, "severity": severity, "drop": drop,
        "live_accuracy": live_accuracy, "trained_accuracy": trained_accuracy,
    }


@shared_task
def retrain_win_probability_model():
    """
    Daily retrain of the win-probability ML model (config/celery.py
    beat_schedule: 16:30 IST, right after monitor_ml_model_performance)
    -- see apps.learning.ml_train.train_win_probability_model for the
    actual training logic. Runs daily, not weekly, so the model
    reflects every closed trade's real outcome as soon as the next day
    (an earlier version of this docstring said "weekly" -- that never
    matched the actual beat schedule; fixed to describe reality).
    Safe to call repeatedly: it's a documented no-op (returns
    trained=False with a reason) rather than an error until
    apps.learning.ml_train.MIN_TRAINING_SAMPLES closed trades exist,
    which is expected during the first weeks of paper trading.
    """
    from apps.learning.ml_train import train_win_probability_model

    return train_win_probability_model()


@shared_task
def retrain_scalp_win_probability_model():
    """
    Daily retrain of the SCALPING win-probability model (config/celery.py
    beat_schedule, right after evaluate-scalping-strategy-methods-daily) --
    see apps.learning.scalp_ml_train.train_scalp_win_probability_model for
    the training logic and why this is a separate model from
    retrain_win_probability_model's, trained only on closed scalping
    HypotheticalTrade rows. Same no-op-until-enough-data safety as that
    task -- returns trained=False with a reason rather than erroring until
    apps.learning.scalp_ml_train.MIN_TRAINING_SAMPLES closed scalps exist.
    """
    from apps.learning.scalp_ml_train import train_scalp_win_probability_model

    return train_scalp_win_probability_model()


@shared_task
def retrain_technical_direction_models():
    """
    Weekly retrain of apps.learning.ml_technical's technical-direction
    model (config/celery.py beat_schedule) -- one model per
    symbol x timeframe combo, over settings.WATCHLIST x
    settings.CHART_TIMEFRAMES (the exact same universe
    apps.market_data.tasks.ingest_watchlist_candles already ingests
    candles for, so every combo this trains against already has data
    flowing in). Previously this model was only reachable by calling
    apps.learning.ml_technical.train_technical_direction_model directly
    from a shell -- this task is what makes it an actual scheduled part
    of the platform rather than a one-off manual step.

    Per-combo failures (e.g. not enough historical candles yet for a
    newly-added timeframe) are caught and logged individually so one
    bad combo can't abort the rest of the sweep -- same "one bad symbol
    doesn't kill the loop" pattern as
    apps.market_data.tasks.ingest_watchlist_candles.
    """
    from django.conf import settings

    from apps.learning.ml_technical import train_technical_direction_model

    results = {}
    for symbol in settings.WATCHLIST:
        for timeframe in settings.CHART_TIMEFRAMES:
            key = f"{symbol}_{timeframe}"
            try:
                results[key] = train_technical_direction_model(symbol, timeframe)
            except Exception:
                logger.exception(
                    "retrain_technical_direction_models: failed for %s %s", symbol, timeframe,
                )
                results[key] = {"trained": False, "reason": "exception"}
    return results


# Matches apps.learning.ml_technical's own max_holding_bars convention
# (its simulated-label walk-forward also caps at 48 bars) -- a
# HypotheticalTrade that never hits its stop or target is closed at
# market after this many bars rather than living forever, same "every
# trade must eventually resolve" reasoning.
STRATEGY_COMPARISON_MAX_HOLDING_BARS = 48
# Scalping trades are meant to resolve in minutes, not the better part
# of an hour -- 48 bars at 1m would let a "scalp" sit open for 48
# minutes, defeating the point. 12 bars (12 minutes) matches the tight
# ATR/SAR-based stops apps.learning.strategy_methods.SCALPING_METHOD_FUNCS
# actually uses.
SCALPING_COMPARISON_MAX_HOLDING_BARS = 12
STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL = 10  # same "don't judge off a tiny sample" bar as MIN_TRADES_FOR_DRIFT_CHECK/MIN_TRAINING_SAMPLES elsewhere in this module

_TIMEFRAME_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "1d": 60 * 24,
}


def _hypothetical_r_multiple(trade) -> float | None:
    """
    Same formula as apps.learning.reward.compute_r_multiple, kept as a
    tiny local reimplementation rather than a shared call -- that
    function takes an apps.execution.models.OpenPosition specifically
    (accesses .unrealized_pnl for a field HypotheticalTrade doesn't
    have, since HypotheticalTrade is deliberately NOT an OpenPosition,
    see that model's own docstring on why) -- duck-typing across two
    intentionally-different shapes would be more fragile than these
    three lines.
    """
    risk_amount = float(trade.qty) * abs(float(trade.entry_price) - float(trade.stop_loss))
    if risk_amount <= 0 or trade.pnl is None:
        return None
    return float(trade.pnl) / risk_amount


def _suggest_option_for_idea(symbol: str, idea: dict) -> tuple[str | None, float | None]:
    """
    Advisory-only CE/PE + strike suggestion attached to a HypotheticalTrade
    at open time (see that model's option_side/strike_price fields for
    why) -- every trader-requested detail ("which contract would I
    actually trade") without changing what the trade simulates against
    (still the underlying's own entry/stop/target). Direction is read
    off the idea itself (stop_loss below entry = long/bullish = CE,
    same convention as apps.signals.engine reading regime/technical
    conditions) rather than hardcoded, so this keeps working correctly
    if a short-side comparison method is ever added to
    apps.learning.strategy_methods -- today all six of them are long-only.

    Never raises and never blocks opening the (real, underlying-based)
    hypothetical trade -- (None, None) whenever no options data is
    synced for this underlying yet, or the strike selector can't score
    anything, same "missing data isn't a hard error" stance
    apps.options.strike_selector.suggest_best_strike's own callers
    already take.
    """
    try:
        from apps.options.signals_engine import nearest_expiry
        from apps.options.strike_selector import suggest_best_strike

        direction = "bullish" if idea["stop_loss"] < idea["entry_price"] else "bearish"
        expiry = nearest_expiry(symbol)
        if expiry is None:
            return None, None
        result = suggest_best_strike(symbol, expiry, direction)
        suggested = result.get("suggested")
        if not suggested:
            return None, None
        option_side = "CE" if direction == "bullish" else "PE"
        return option_side, suggested["strike"]
    except Exception:
        logger.exception("_suggest_option_for_idea failed for %s -- leaving option_side/strike_price null.", symbol)
        return None, None


def _run_comparison_cycle(method_funcs: dict, timeframe: str, max_holding_bars: int, task_name: str) -> dict:
    """
    Shared open/close/timeout cycle for BOTH the 5m swing comparison
    (run_strategy_method_comparison) and the 1m scalping comparison
    (run_scalping_strategy_comparison) below -- the logic is identical
    either way (one open HypotheticalTrade per method+symbol at a time;
    check stop/target/timeout if one's open, otherwise ask the method
    for a fresh idea), only the method group, timeframe, and holding-
    bar cap differ, so it's one implementation instead of two that
    could quietly drift apart.
    """
    from django.conf import settings

    from apps.market_data.models import HistoricalData

    from .models import HypotheticalTrade
    from .strategy_methods import SCALPING_METHOD_FUNCS

    tf_minutes = _TIMEFRAME_MINUTES.get(timeframe, 5)
    results = {"opened": [], "closed": [], "errors": []}

    for method, generate_idea in method_funcs.items():
        for symbol in settings.WATCHLIST:
            try:
                latest = (
                    HistoricalData.objects.filter(symbol=symbol, timeframe=timeframe)
                    .order_by("-timestamp").first()
                )
                if latest is None:
                    continue
                current_price = latest.close

                open_trade = HypotheticalTrade.objects.filter(
                    method=method, symbol=symbol, closed_at__isnull=True,
                ).first()

                if open_trade is not None:
                    exit_price = None
                    exit_reason = ""
                    if current_price <= open_trade.stop_loss:
                        exit_price, exit_reason = open_trade.stop_loss, "stop"
                    elif open_trade.target_price is not None and current_price >= open_trade.target_price:
                        exit_price, exit_reason = open_trade.target_price, "target"
                    else:
                        bars_held = (timezone.now() - open_trade.opened_at).total_seconds() / 60 / tf_minutes
                        if bars_held >= max_holding_bars:
                            exit_price, exit_reason = current_price, "timeout"

                    if exit_price is not None:
                        open_trade.exit_price = exit_price
                        open_trade.exit_reason = exit_reason
                        open_trade.pnl = (exit_price - open_trade.entry_price) * open_trade.qty
                        open_trade.closed_at = timezone.now()
                        open_trade.r_multiple = _hypothetical_r_multiple(open_trade)
                        open_trade.save(update_fields=["exit_price", "exit_reason", "pnl", "closed_at", "r_multiple"])
                        results["closed"].append({"method": method, "symbol": symbol, "reason": exit_reason, "pnl": float(open_trade.pnl)})
                    continue

                idea = generate_idea(symbol, timeframe)
                if idea is None:
                    continue
                option_side, strike_price = _suggest_option_for_idea(symbol, idea)

                # "ind" is only present for SCALPING_METHOD_FUNCS ideas
                # (see strategy_methods.py's module docstring) -- pop it
                # out before spreading `idea` into create() below (that
                # dict must only contain real HypotheticalTrade fields)
                # and convert it to the ind_* columns apps.learning.
                # scalp_ml_train trains on, via the exact same helper
                # apps.signals.engine uses for TradingSignal's identical
                # columns, so the two never normalize NaN/scale
                # differently.
                ind = idea.pop("ind", None)
                ind_fields = {}
                if ind is not None:
                    from apps.signals.engine import _indicator_signal_fields

                    ind_fields = _indicator_signal_fields(ind)

                trade = HypotheticalTrade.objects.create(
                    method=method, symbol=symbol, timeframe=timeframe,
                    option_side=option_side, strike_price=strike_price,
                    **idea, **ind_fields,
                )

                if method in SCALPING_METHOD_FUNCS:
                    from .scalp_ml_predict import predict_scalp_win_probability

                    probability = predict_scalp_win_probability(trade)
                    if probability is not None:
                        trade.ml_win_probability = probability
                        trade.save(update_fields=["ml_win_probability"])

                results["opened"].append({"method": method, "symbol": symbol, "id": trade.pk})
            except Exception:
                logger.exception("%s: failed for %s/%s", task_name, method, symbol)
                results["errors"].append(f"{method}/{symbol}")

    return results


@shared_task
def run_strategy_method_comparison(timeframe: str = "5m"):
    """
    Runs apps.learning.strategy_methods' three SWING comparison
    strategies (trend-following, mean-reversion, breakout) continuously
    alongside the real signal engine (config/celery.py beat_schedule,
    same 300s/5min cadence as generate-signals-every-5-minutes) -- see
    apps/learning/models.py's HypotheticalTrade docstring for why this
    is a fully isolated paper-only simulation that never touches
    apps.execution/apps.risk/real money, regardless of the platform's
    real EXECUTION_MODE.

    Same market-hours gate as apps.signals.tasks.generate_signals_for_watchlist
    (there's no real price action to simulate against outside NSE
    hours), and the same per-item try/except-and-continue pattern as
    retrain_technical_direction_models so one bad symbol/method combo
    can't abort the sweep.
    """
    from apps.market_data.market_hours import is_market_open

    from .strategy_methods import METHOD_FUNCS

    is_open, reason = is_market_open()
    if not is_open:
        logger.info("run_strategy_method_comparison: market closed (%s) -- skipping.", reason)
        return {"skipped": True, "reason": reason}

    return _run_comparison_cycle(
        METHOD_FUNCS, timeframe, STRATEGY_COMPARISON_MAX_HOLDING_BARS, "run_strategy_method_comparison",
    )


@shared_task
def run_scalping_strategy_comparison(timeframe: str = "1m"):
    """
    Same isolated paper-only comparison as run_strategy_method_comparison
    above, but for apps.learning.strategy_methods' three SCALPING
    methods (ema-momentum, rsi-extreme, sar-volume-burst) on the 1m
    timeframe -- its own beat schedule entry (every 1 minute, matching
    ingest-watchlist-1m-every-minute's cadence, since checking a scalp's
    stop/target only every 5 minutes would defeat the point of scalping)
    and its own, much shorter SCALPING_COMPARISON_MAX_HOLDING_BARS.
    """
    from apps.market_data.market_hours import is_market_open

    from .strategy_methods import SCALPING_METHOD_FUNCS

    is_open, reason = is_market_open()
    if not is_open:
        logger.info("run_scalping_strategy_comparison: market closed (%s) -- skipping.", reason)
        return {"skipped": True, "reason": reason}

    return _run_comparison_cycle(
        SCALPING_METHOD_FUNCS, timeframe, SCALPING_COMPARISON_MAX_HOLDING_BARS, "run_scalping_strategy_comparison",
    )


@shared_task
def run_scalping_real_execution():
    """
    Real (paper-mode) option-position execution for the scalping methods
    -- an ADDITIONAL path alongside run_scalping_strategy_comparison
    above, not a replacement (that task and its HypotheticalTrade
    comparison data are completely untouched by this one). See
    apps.learning.scalp_execution's module docstring for the full
    reasoning, including why real trades from this task now compete with
    apps.options.index_direction_strategy's own signals for the shared
    one-open-position-per-symbol risk-engine slot.

    Own 1-minute beat schedule entry, same cadence as
    run_scalping_strategy_comparison, for the same reason (checking a
    scalp's stop/target only every 5 minutes would defeat the point) --
    exit-checking is apps.execution.paper_executor.check_and_close_positions,
    already option-contract-aware and safe to call repeatedly, run once
    here after every method/symbol combo has had a chance to open.
    """
    from apps.market_data.market_hours import is_market_open
    from apps.execution.models import ExecutionModeSetting, get_execution_mode

    if get_execution_mode() != ExecutionModeSetting.Mode.PAPER:
        logger.warning("run_scalping_real_execution: disabled outside paper execution mode.")
        return {"skipped": True, "reason": "paper_only"}

    from .scalp_execution import evaluate_and_open_scalp
    from .strategy_methods import SCALPING_METHOD_FUNCS

    is_open, reason = is_market_open()
    if not is_open:
        logger.info("run_scalping_real_execution: market closed (%s) -- skipping.", reason)
        return {"skipped": True, "reason": reason}

    from django.conf import settings

    results = {"signals": [], "errors": []}
    for method, generate_idea in SCALPING_METHOD_FUNCS.items():
        for symbol in settings.WATCHLIST:
            symbol = symbol.strip()
            try:
                signal = evaluate_and_open_scalp(method, generate_idea, symbol)
                if signal is not None:
                    results["signals"].append({
                        "method": method, "symbol": symbol,
                        "status": signal.status, "signal_id": signal.pk,
                    })
            except Exception:
                logger.exception("run_scalping_real_execution: failed for %s/%s", method, symbol)
                results["errors"].append(f"{method}/{symbol}")

    from apps.execution.paper_executor import check_and_close_positions

    results["position_updates"] = check_and_close_positions("1m")
    return results


def _evaluate_method_group(method_funcs: dict, task_name: str) -> dict:
    """
    Shared aggregation for BOTH evaluate_strategy_methods (swing) and
    evaluate_scalping_strategy_methods (scalping) below: rolls up each
    method's closed HypotheticalTrade rows into win_rate/avg_r_multiple/
    total_pnl/trade_count and records them via apps.learning.models.
    ModelRegistry -- reusing that table exactly as designed (a free-
    form, versioned, champion-flagged registry) rather than inventing a
    parallel "memory" store. model_name convention:
    f"strategy_comparison_{method}". active_flag=True marks whichever
    method in THIS group currently has the best win_rate -- deliberately
    scoped per group (not compared globally across both the 5m swing
    and 1m scalping methods at once): a scalp's win_rate and a swing
    trade's win_rate aren't measuring the same kind of bet, so "best
    scalping method" and "best swing method" are the two questions each
    group's own page actually wants answered, not one combined ranking.

    A method needs >= STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL closed
    trades before it's even included -- same "don't judge off a tiny,
    noisy sample" stance as MIN_TRADES_FOR_DRIFT_CHECK/MIN_TRAINING_SAMPLES
    elsewhere in this module. If no method has enough trades yet
    (expected during the first days this runs), this is a documented
    no-op, not an error.
    """
    from django.db.models import Avg, Sum

    from .models import HypotheticalTrade, ModelRegistry

    stats = {}
    for method in method_funcs:
        closed = HypotheticalTrade.objects.filter(method=method, closed_at__isnull=False)
        trade_count = closed.count()
        if trade_count < STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL:
            continue
        wins = closed.filter(pnl__gt=0).count()
        avg_r = closed.exclude(r_multiple__isnull=True).aggregate(avg=Avg("r_multiple"))["avg"]
        total_pnl = closed.aggregate(total=Sum("pnl"))["total"]
        stats[method] = {
            "trade_count": trade_count,
            "win_rate": wins / trade_count,
            "avg_r_multiple": avg_r,
            "total_pnl": float(total_pnl) if total_pnl is not None else 0.0,
        }

    if not stats:
        logger.info(
            "%s: no method has >= %d closed trades yet -- nothing to evaluate.",
            task_name, STRATEGY_COMPARISON_MIN_TRADES_FOR_EVAL,
        )
        return {"skipped": True, "reason": "insufficient_sample"}

    best_method = max(stats, key=lambda m: stats[m]["win_rate"])
    version = timezone.now().strftime("%Y%m%d%H%M%S")

    ModelRegistry.objects.filter(
        model_name__in=[f"strategy_comparison_{m}" for m in method_funcs], active_flag=True,
    ).update(active_flag=False)

    for method, metrics in stats.items():
        ModelRegistry.objects.create(
            model_name=f"strategy_comparison_{method}",
            model_version=version,
            artifact_path="",
            metrics_json=metrics,
            active_flag=(method == best_method),
        )

    return {"stats": stats, "best_method": best_method}


@shared_task
def evaluate_strategy_methods():
    """Daily (config/celery.py beat_schedule) evaluation of the 5m swing comparison methods -- see _evaluate_method_group."""
    from .strategy_methods import METHOD_FUNCS

    return _evaluate_method_group(METHOD_FUNCS, "evaluate_strategy_methods")


@shared_task
def evaluate_scalping_strategy_methods():
    """Daily (config/celery.py beat_schedule) evaluation of the 1m scalping comparison methods -- see _evaluate_method_group."""
    from .strategy_methods import SCALPING_METHOD_FUNCS

    return _evaluate_method_group(SCALPING_METHOD_FUNCS, "evaluate_scalping_strategy_methods")
