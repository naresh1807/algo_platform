"""
Intent Detection (manual 14.23 priority #2).

Honest scope note: this is a deliberate rule-based keyword/phrase
matcher, not a trained NLU model or an LLM call. For a fixed, fairly
small command vocabulary (manual 14.8-14.15's ~45 commands) a keyword
matcher is transparent, needs no API key/model download, and is
trivially testable -- every phrase->action mapping is a plain Python
dict anyone can read. The trade-off is real: it will not understand
free-form phrasing outside the patterns below ("what's my drawdown
looking like today" won't match "show drawdown" as written). Swapping
this module's `detect_intent()` for an LLM-backed classifier later is
a contained change -- callers (engine.py) only depend on this
function's return shape, not on how it's implemented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.conf import settings

from .commands import COMMANDS

# Longer/more specific phrases must be tried before shorter/more
# generic ones from the same category, or a generic match ("show ")
# would shadow a specific one ("show risk"). Sorting once at import
# time (not per-call) keeps detect_intent() cheap.
_RANKED_PHRASES: list[tuple[str, str]] = sorted(
    (
        (phrase, action)
        for action, spec in COMMANDS.items()
        for phrase in spec["phrases"]
    ),
    key=lambda pair: -len(pair[0]),
)

# manual 14.9: "Show NIFTY" / "Show BANKNIFTY" -- symbol entities come
# from the real configured watchlist, not a hardcoded guess, so a
# custom WATCHLIST in .env is immediately recognized too.
def _known_symbols() -> list[str]:
    return [s.strip().upper() for s in getattr(settings, "WATCHLIST", []) if s.strip()]


@dataclass
class DetectedIntent:
    action: str | None
    category: str | None
    confidence: float
    entities: dict = field(default_factory=dict)
    matched_phrase: str | None = None


def detect_intent(text: str) -> DetectedIntent:
    """
    Returns the best-matching action for `text`, or action=None if
    nothing in the registry matches closely enough. Confidence is a
    simple, explainable heuristic (1.0 for an exact phrase match, 0.7
    for a substring match) -- deliberately not a calibrated probability
    like apps.learning's ML model; it exists only to decide whether
    engine.py should ask "did you mean...?" instead of guessing.
    """
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return DetectedIntent(action=None, category=None, confidence=0.0)

    entities = _extract_entities(normalized)

    if normalized in {p for p, _ in _RANKED_PHRASES}:
        for phrase, action in _RANKED_PHRASES:
            if phrase == normalized:
                return DetectedIntent(
                    action=action, category=COMMANDS[action]["category"],
                    confidence=1.0, entities=entities, matched_phrase=phrase,
                )

    for phrase, action in _RANKED_PHRASES:
        # Word-boundary match, not a plain substring check -- a bare `in`
        # test lets a short registered phrase match inside an unrelated
        # longer word (e.g. go_back's "back" matching inside "backflip",
        # turning "do a backflip" into a false-positive navigation
        # command). \b around the phrase keeps the same "find this
        # phrase anywhere in the sentence" behavior for genuine word/
        # phrase matches (multi-word phrases like "show risk" still match
        # as a run of words) while refusing to match mid-word.
        if re.search(rf"\b{re.escape(phrase.strip())}\b", normalized):
            return DetectedIntent(
                action=action, category=COMMANDS[action]["category"],
                confidence=0.7, entities=entities, matched_phrase=phrase,
            )

    return DetectedIntent(action=None, category=None, confidence=0.0, entities=entities)


def _extract_entities(normalized_text: str) -> dict:
    entities = {"raw_text": normalized_text}
    for symbol in _known_symbols():
        if re.search(rf"\b{re.escape(symbol.lower())}\b", normalized_text):
            entities["symbol"] = symbol
            break

    # A bare number in the utterance -- used by price-alert creation
    # ("alert me when nifty above 25000") and any other command that
    # might need a numeric argument later. Takes the first number found
    # (commas allowed, e.g. "25,000") rather than trying to disambiguate
    # multiple numbers in one phrase -- a genuinely ambiguous utterance
    # ("alert between 24000 and 25000") isn't something this rule-based
    # matcher should guess at; see module docstring's honest scope note.
    price_match = re.search(r"\d[\d,]*\.?\d*", normalized_text)
    if price_match:
        try:
            entities["price"] = float(price_match.group().replace(",", ""))
        except ValueError:
            pass

    return entities
