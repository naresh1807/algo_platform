"""
FinBERT wrapper (manual section 4: "AI sentiment stack"). Uses
ProsusAI/finbert -- a BERT model fine-tuned specifically on financial
text, which matters here: general-purpose sentiment models routinely
get financial language backwards (e.g. "shares tumbled" scored as
neutral/objective rather than negative, or "beat expectations" scored
lower than its actual positivity because "beat" alone often reads as
violent/negative in general-purpose training data).

Model loading is expensive (downloads/loads real weights) and this
module is imported by apps.news.tasks, which Django's Celery worker
imports at startup -- so the model is lazy-loaded on first actual use
(_get_pipeline()), not at import time, exactly the same pattern as
apps.market_data.broker_client's lazy SmartConnect import. This means
importing apps.news.finbert is always cheap; only calling score_headline()
triggers the (one-time, then cached) model load.
"""

from __future__ import annotations

import logging

from common.constants import SentimentLabel

logger = logging.getLogger(__name__)

MODEL_NAME = "ProsusAI/finbert"

_pipeline = None  # module-level cache -- loaded once per process, reused across calls


def _get_pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    # Imported lazily: transformers + torch are large, and a Django
    # process that never actually scores a headline (e.g. running only
    # the DRF API, not a Celery worker) shouldn't pay for importing them.
    from transformers import pipeline

    logger.info("Loading FinBERT model (%s) -- this can take a while on first run.", MODEL_NAME)
    _pipeline = pipeline("sentiment-analysis", model=MODEL_NAME, tokenizer=MODEL_NAME)
    return _pipeline


# FinBERT's own output labels -> this project's SentimentLabel choices.
# Kept as an explicit mapping (not assuming FinBERT's casing/spelling
# matches ours) since a silent mismatch here would mean every headline
# gets mis-labeled without any error being raised.
_LABEL_MAP = {
    "positive": SentimentLabel.POSITIVE,
    "negative": SentimentLabel.NEGATIVE,
    "neutral": SentimentLabel.NEUTRAL,
}


def score_headline(text: str) -> dict:
    """
    Returns {"label": SentimentLabel, "score": float (-1..+1), "confidence": float (0..1)}.

    FinBERT's raw output is a label + a confidence in THAT label (e.g.
    {"label": "positive", "score": 0.87}) -- it does not itself produce
    a signed -1..+1 scale. This function converts: positive confidence
    maps to a positive signed score, negative confidence maps to a
    negative signed score, and neutral maps to 0.0 regardless of the
    model's confidence in "neutral" (a highly-confident neutral
    headline should score as genuinely neutral, not as a strong
    signal in either direction).
    """
    if not text or not text.strip():
        return {"label": SentimentLabel.NEUTRAL, "score": 0.0, "confidence": 0.0}

    classifier = _get_pipeline()
    # truncation=True guards against headlines/snippets longer than
    # FinBERT's 512-token limit throwing an error instead of just
    # truncating -- news snippets are normally short, but defensive here
    # costs nothing.
    result = classifier(text, truncation=True)[0]

    raw_label = result["label"].lower()
    confidence = float(result["score"])
    label = _LABEL_MAP.get(raw_label, SentimentLabel.NEUTRAL)

    if label == SentimentLabel.POSITIVE:
        signed_score = confidence
    elif label == SentimentLabel.NEGATIVE:
        signed_score = -confidence
    else:
        signed_score = 0.0

    return {"label": label, "score": round(signed_score, 4), "confidence": round(confidence, 4)}
