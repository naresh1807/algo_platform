"""
JARVIS's free-form fallback: a self-hosted, open-source LLM running
locally via Ollama (https://ollama.com) -- NOT a paid external API.
Used ONLY when apps.jarvis.intent's rule-based matcher finds no
command match for what was typed/said (see engine.py's process()) --
every recognized command still goes through the deterministic,
fully-explainable handler pipeline in responses.py exactly as before.
This module exists so "I didn't recognize that command" isn't the only
possible answer to a real question, not to replace the command system.

WHY LOCAL INSTEAD OF AN API: no per-call cost, no data leaving the
machine, and it matches this project's established "self-host what you
can" pattern (apps.news.rss_client over paid NewsAPI, Angel One's own
instrument master over scraping nseindia.com) -- discussed and chosen
deliberately over adding an external LLM API dependency.

HONESTY BOUNDARIES (same "AI must explain every signal" principle this
whole codebase holds everywhere else): this module has READ-ONLY
access to exactly three things -- recent candles (apps.market_data.
HistoricalData), recent news sentiment (apps.news.NewsSentiment), both
for settings.WATCHLIST symbols only, and the CURRENT STATUS of
apps.learning's already-existing, already-autonomous ML pipeline
(which model is active and its recorded metrics, the most recent drift
check, the most recent daily review) -- fetched fresh on every call by
_build_grounding_context()/_build_learning_context() below and handed
to the model as explicit context it's told to use ONLY those given
numbers from, never extrapolate beyond. It still has NO access to
portfolio/P&L, positions, risk figures, account balance, or anything
else in this platform's database, and NO write access to anything,
ever -- for those, the system prompt tells it to redirect to the real
command/page exactly as before this change. A free-form answer is
labeled as such by the caller (engine.py sets action="ai_freeform" on
the response) rather than presented indistinguishably from a real,
data-backed command response.

ON "SELF LEARNING": apps.learning.ml_train already retrains its models
and apps.learning.tasks already runs drift detection on a fixed Celery
schedule, autonomously, completely independent of this module --
that's real, pre-existing, evidence-gated automation (see
DailyReviewNote's own docstring: suggested changes still need a human
to set approved_flag=True before anything acts on them). This module
does not add any NEW autonomous behavior -- it only narrates that
existing pipeline's current state in chat. The LLM itself is never
trained/fine-tuned here, has no memory of past answers feeding future
ones, and cannot change ANY of these numbers, model weights, or
thresholds -- "self learning," as implemented, means the data it
narrates evolves on its own existing schedule, not that the assistant
modifies itself or anything else.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 75  # CPU-only local inference is slow, and Ollama unloads an idle model from memory after a few minutes (its own default keep_alive) -- a cold-start reload plus generation measured ~30-40s on this hardware even before grounding context existed. Bumped from the original 45s once _build_learning_context() (below) started routinely pushing warm-model generation to ~40s on its own -- 45s was cutting it too close and started producing real, reproducible timeouts, not just a dead-Ollama safety margin.
MAX_ANSWER_CHARS = 800  # This is a chat panel, not a document -- keeps a rambling model output from dominating the JARVIS panel.

CANDLES_PER_SYMBOL = 6  # a short recent timeline, not a full chart -- enough to say "up/down over the last half hour," not meant to replace the actual Dashboard chart.
NEWS_LOOKBACK = timedelta(hours=3)
NEWS_ITEM_LIMIT = 6
DRIFT_LOOKBACK = timedelta(days=7)

SYSTEM_PROMPT = (
    "You are JARVIS, the assistant panel inside a personal algo-trading platform "
    "that trades NIFTY/BANKNIFTY index options in India (paper trading by default, "
    "real trades only when explicitly enabled). You are a FALLBACK for questions "
    "that don't match this platform's built-in commands.\n\n"
    "This platform ACTUALLY HAS these real, working features -- if asked about any "
    "of them, confidently point to the specific command/page below, never hedge with "
    "\"if it exists\" or guess that a feature might not be there:\n"
    "- News + sentiment on Indian markets, refreshed every 5 minutes (NOT real-time/"
    "instant -- don't call it real-time): say \"ask me for a market summary, or check "
    "the News page in the sidebar\" -- this is real, backed by RSS feeds and FinBERT "
    "sentiment scoring, not a stub.\n"
    "- Portfolio/P&L: \"today's profit\", \"open positions\", \"closed trades\"\n"
    "- Risk: \"show risk\", \"show drawdown\", \"kill switch status\"\n"
    "- Live price/chart data: \"current price\", \"market status\", or the Dashboard page\n"
    "- Options chain/analytics: the Options Analytics page\n"
    "- AI/learning system status: \"ai status\", \"learning progress\" -- and the context "
    "block below may already include real active-model/drift/daily-review info, so "
    "answer directly from that when it's present instead of only redirecting.\n\n"
    "Hard rules:\n"
    "- Each message may include a \"Current market context\" block with real recent "
    "candle prices, news headlines, and ML learning-pipeline status (active model "
    "metrics, drift checks, daily review), fetched fresh from the database just for "
    "this question. When it's there, you MAY use those specific numbers/facts directly "
    "-- but ONLY the ones actually given, never extrapolate or invent additional "
    "prices/headlines/metrics beyond what's in that block.\n"
    "- Beyond that block, you do NOT have access to this platform's live database -- "
    "never invent specific numbers for P&L, position sizes, risk figures, account "
    "balance, or anything not in the context block. For those, redirect to the real "
    "command/page above, stated confidently since you know it exists -- don't just say "
    "\"I don't have access\" and stop there.\n"
    "- If asked about current price/news and the context block is empty or missing, say "
    "so honestly (e.g. markets may be closed or no recent data yet) rather than guessing.\n"
    "- You cannot execute any action (place trades, change settings, close positions). "
    "Never claim to have done so.\n"
    "- Keep answers short and direct -- 2-4 sentences, this is a chat panel not an essay.\n"
    "- For general/explanatory questions (market concepts, what a term means, general "
    "trading education) answer normally and helpfully -- that part of your job doesn't "
    "need the platform's data at all."
)


def is_configured() -> bool:
    """
    Cheap reachability check (not a guarantee it'll stay up for the
    actual request) -- used by responses/engine to decide whether to
    even attempt the fallback vs. going straight to the canned
    not-recognized message, so a machine without Ollama running doesn't
    pay a timeout on every single unrecognized command.
    """
    try:
        response = requests.get(f"{settings.OLLAMA_BASE_URL}/api/version", timeout=2)
        return response.status_code == 200
    except Exception:
        return False


def _build_grounding_context() -> str:
    """
    Compact, real, freshly-queried recent candles + news for
    settings.WATCHLIST symbols, plus apps.learning's current ML
    pipeline status (see _build_learning_context() below) -- this is
    the entire "database access" this module has (see the module
    docstring's HONESTY BOUNDARIES). Deliberately small (CANDLES_PER_SYMBOL/NEWS_ITEM_LIMIT) and computed
    on every call rather than cached: CPU-only local inference is
    already slow (see REQUEST_TIMEOUT_SECONDS), so a large context would
    only make it slower for no real benefit, and caching would risk
    handing the model stale numbers it then presents as current --
    exactly the failure mode grounding it in real data is meant to
    prevent. Returns "" (not None) when there's nothing recent to
    report (e.g. market closed, no ingestion has run yet) -- the system
    prompt tells the model to say so honestly rather than treating an
    empty string as "context wasn't provided."
    """
    from apps.market_data.models import HistoricalData
    from apps.news.models import NewsSentiment
    from django.utils import timezone

    lines: list[str] = []

    for symbol in settings.WATCHLIST:
        candles = list(
            HistoricalData.objects.filter(symbol=symbol, timeframe="5m")
            .order_by("-timestamp")[:CANDLES_PER_SYMBOL]
        )
        if not candles:
            continue
        candles.reverse()  # oldest -> newest, readable as a short timeline
        closes = " -> ".join(f"{float(c.close):.2f}" for c in candles)
        first_open, last_close = float(candles[0].open), float(candles[-1].close)
        change_pct = ((last_close - first_open) / first_open * 100) if first_open else None
        change_str = f" ({change_pct:+.2f}% over this window)" if change_pct is not None else ""
        lines.append(f"{symbol} last {len(candles)}x5m candle closes: {closes}{change_str}")

    since = timezone.now() - NEWS_LOOKBACK
    news_items = (
        NewsSentiment.objects.filter(published_at__gte=since, symbol__in=settings.WATCHLIST)
        .order_by("-published_at")[:NEWS_ITEM_LIMIT]
    )
    if news_items:
        lines.append(f"Recent news (last {int(NEWS_LOOKBACK.total_seconds() // 3600)}h):")
        for n in news_items:
            lines.append(f"- [{n.symbol}] {n.sentiment_label} ({n.sentiment_score:+.2f}): {n.headline}")

    lines.extend(_build_learning_context())

    return "\n".join(lines)


def _build_learning_context() -> list[str]:
    """
    Narrates apps.learning's ALREADY-EXISTING, ALREADY-AUTONOMOUS ML
    pipeline (see the module docstring's "ON SELF LEARNING" section) --
    active model(s) + a few key recorded metrics, the most recent drift
    check, and the most recent daily review. Read-only, same as
    everything else in this module: this function only reads rows
    apps.learning's own scheduled tasks already wrote; it never
    triggers training, never writes anything, and the LLM narrating
    this data cannot feed back into it.

    Only a handful of metrics_json keys are surfaced (accuracy/roc_auc,
    row_count, promotion status), not the whole JSON blob --
    ModelRegistry.metrics_json also carries full feature_importances/
    feature_columns dumps (dozens of keys) that would bloat the prompt
    for no benefit to a chat answer, directly against this module's own
    "keep it small" design (see _build_grounding_context's docstring).
    confidence_band() (apps.learning.confidence) is deliberately NOT
    used here -- it's calibrated for a per-signal win PROBABILITY
    (TradingSignal.ml_win_probability), not a model's overall accuracy/
    roc_auc, which is a different scale/meaning; applying it here would
    produce a misleading label rather than an honest one.
    """
    from apps.learning.models import DailyReviewNote, DriftEvent, ModelRegistry
    from django.utils import timezone

    lines: list[str] = []

    active_models = ModelRegistry.objects.filter(active_flag=True).order_by("model_name")
    if active_models:
        lines.append("Active ML models:")
        for m in active_models:
            metrics = m.metrics_json or {}
            key_metric = metrics.get("roc_auc") or metrics.get("accuracy") or metrics.get("win_probability")
            metric_str = f"key metric {key_metric:.3f}" if key_metric is not None else "no recorded metric"
            row_count = metrics.get("row_count")
            rows_str = f", trained on {row_count} rows" if row_count is not None else ""
            lines.append(f"- {m.model_name} v{m.model_version}: {metric_str}{rows_str}")
    else:
        lines.append("Active ML models: none trained/activated yet.")

    since = timezone.now() - DRIFT_LOOKBACK
    recent_drift = DriftEvent.objects.filter(detected_at__gte=since).order_by("-detected_at").first()
    if recent_drift:
        lines.append(
            f"Most recent drift check ({recent_drift.detected_at.date()}): "
            f"{recent_drift.drift_type}/{recent_drift.metric_name} -- severity {recent_drift.severity}"
            + (f", action taken: {recent_drift.action_taken}" if recent_drift.action_taken else "")
        )

    latest_review = DailyReviewNote.objects.order_by("-review_date").first()
    if latest_review:
        approved = "approved" if latest_review.approved_flag else "pending human approval"
        lines.append(f"Latest daily review ({latest_review.review_date}, {approved}): {latest_review.summary}")

    return lines


def ask(question: str) -> str | None:
    """
    Returns the model's answer, or None if Ollama isn't reachable, the
    request times out, or the model errors -- callers (engine.py) treat
    None exactly like "no local LLM available" and fall back to the
    standard not-recognized message. Never raises -- a local model
    being down must not turn into a 500 for the whole JARVIS chat.
    """
    try:
        context = _build_grounding_context()
    except Exception:
        # A DB hiccup while building context must not take down the
        # whole fallback -- same "never raises" guarantee this
        # function's own docstring makes for the Ollama call below,
        # just degrading to "no context" instead of "no answer at all."
        logger.exception("local_llm._build_grounding_context failed -- asking without market context.")
        context = ""

    user_content = (
        f"Current market context (real data, use ONLY these numbers for anything specific):\n{context}\n\nQuestion: {question}"
        if context
        else question
    )

    try:
        response = requests.post(
            f"{settings.OLLAMA_BASE_URL}/api/chat",
            json={
                "model": settings.OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "stream": False,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        answer = response.json().get("message", {}).get("content", "").strip()
        if not answer:
            return None
        return answer[:MAX_ANSWER_CHARS]
    except requests.exceptions.Timeout:
        logger.warning("local_llm.ask: Ollama request timed out after %ss.", REQUEST_TIMEOUT_SECONDS)
        return None
    except Exception:
        logger.exception("local_llm.ask: request to Ollama failed.")
        return None
