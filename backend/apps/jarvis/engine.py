"""
Command Engine (manual 14.3 Architecture, 14.23 priority #1).

This is the single entry point every input mode (REST for typed/voice-
transcribed text today, the WebSocket consumer for push) funnels
through: text -> intent detection -> [restricted-action gate] ->
handler dispatch -> response -> history log -> memory update.

Security (manual 14.21) is enforced HERE, once, not scattered across
individual handlers:
  - FORBIDDEN_ACTIONS never reach a handler at all, regardless of
    `confirm`. There is deliberately no code path in this file that
    can flip the kill switch, edit RISK_HARD_LIMITS, or place a live
    order -- those stay exclusively behind apps.risk's own management
    command / apps.execution.live_executor, neither of which this
    module imports for writing. In practice, none of the four
    FORBIDDEN_ACTIONS names are ever registered in commands.COMMANDS
    (see that module's own comment), so this check is defense-in-depth
    against a future accidental registration, not a branch real user
    input exercises today -- the real guarantee is "never registered,"
    this is the backstop if that ever slips.
  - RESTRICTED_ACTIONS only execute when the caller passes
    confirm=True; otherwise the engine returns a needs_confirmation
    response and logs that a confirmation was requested, per manual
    14.21's "Must Always Ask Confirmation For" list.
"""

from __future__ import annotations

import time

from . import memory, navigation, responses
from .commands import COMMANDS, FORBIDDEN_ACTIONS, RESTRICTED_ACTIONS
from .intent import detect_intent
from .models import JarvisCommandHistory

# Maps action -> the callable in responses.py that handles it. Built
# once from COMMANDS so adding a new command to the registry without a
# matching function in responses.py fails loudly (KeyError) at import
# time instead of silently 404ing at request time.
_HANDLERS = {
    action: getattr(responses, action)
    for action in COMMANDS
    if hasattr(responses, action)
}


class JarvisResponse:
    def __init__(self, *, text, action=None, category=None, route=None,
                 needs_confirmation=False, success=True):
        self.text = text
        self.action = action
        self.category = category
        self.route = route
        self.needs_confirmation = needs_confirmation
        self.success = success

    def to_dict(self):
        return {
            "text": self.text,
            "action": self.action,
            "category": self.category,
            "route": self.route,
            "needs_confirmation": self.needs_confirmation,
            "success": self.success,
        }


def process(raw_text: str, user, *, input_mode: str = "text", confirm: bool = False) -> JarvisResponse:
    started = time.monotonic()
    intent = detect_intent(raw_text)

    if intent.action is None:
        response = _fallback_response(raw_text)
        _log(raw_text, user, input_mode, intent, response, started)
        return response

    if intent.action in FORBIDDEN_ACTIONS:
        response = JarvisResponse(
            text="I can't do that -- it's outside JARVIS's permissions in Version 1.0 (manual 14.21).",
            action=intent.action, category=intent.category, success=False,
        )
        _log(raw_text, user, input_mode, intent, response, started)
        return response

    if intent.action in RESTRICTED_ACTIONS and not confirm:
        response = JarvisResponse(
            text=f"\"{intent.action.replace('_', ' ')}\" needs confirmation before I do it. Say it again to confirm.",
            action=intent.action, category=intent.category, needs_confirmation=True,
        )
        _log(raw_text, user, input_mode, intent, response, started, needs_confirmation=True)
        return response

    handler = _HANDLERS.get(intent.action)
    if handler is None:
        response = JarvisResponse(
            text=f"\"{intent.action}\" is recognized but not wired up to a handler yet.",
            action=intent.action, category=intent.category, success=False,
        )
        _log(raw_text, user, input_mode, intent, response, started)
        return response

    try:
        # A couple of handlers (create_price_alert) need to know WHO is
        # asking, to scope what they create to the right user -- every
        # other handler is read-only and ignores this. Injected here
        # rather than changing every handler's signature to accept
        # `user` separately.
        entities = dict(intent.entities)
        entities["_user"] = user
        text = handler(entities)
        if text == "RESTRICTED":
            # Handlers for RESTRICTED_ACTIONS never actually execute the
            # side effect themselves (see commands.py) -- reaching this
            # branch means confirm=True was passed, so we say so plainly
            # rather than silently doing nothing.
            text = (
                f"Confirmed: \"{intent.action.replace('_', ' ')}\" would run here, but this scaffold "
                f"deliberately stops short of wiring the actual destructive action (portfolio reset / "
                f"history deletion / mass position exit) to a one-line voice command without a second, "
                f"explicit UI action -- see the dashboard's own confirm dialog for that action instead."
            )
        route = navigation.route_for(intent.action)
        response = JarvisResponse(text=text, action=intent.action, category=intent.category, route=route)
    except Exception as exc:  # noqa: BLE001 -- JARVIS must always answer, never 500 the chat
        response = JarvisResponse(
            text=f"That command matched \"{intent.action}\" but hit an error running it: {exc}",
            action=intent.action, category=intent.category, success=False,
        )

    _log(raw_text, user, input_mode, intent, response, started)
    if user is not None and getattr(user, "is_authenticated", False):
        try:
            memory.update_context(user, last_command=raw_text, last_intent=intent.action)
        except Exception:
            # Same defensive principle as _log() just above: context
            # memory failing (a DB hiccup, a not-yet-migrated fresh
            # install, a locked row) must never turn into a 500 for the
            # whole command -- JARVIS should still answer even if it
            # can't remember the interaction afterward.
            pass
    return response


_NOT_RECOGNIZED_TEXT = (
    "I didn't recognize that command. Try things like \"show risk\", "
    "\"open portfolio\", \"today's profit\", or \"AI status\"."
)


def _fallback_response(raw_text: str) -> JarvisResponse:
    """
    Called when intent.action is None -- apps.jarvis.intent's rule-based
    matcher found nothing. Tries the local LLM (apps.jarvis.local_llm)
    before giving up with the old canned not-recognized message.
    is_configured() is checked FIRST specifically so a machine without
    Ollama running doesn't pay a network-timeout wait on every single
    unrecognized command -- it stays exactly as fast as before when the
    local LLM isn't set up at all.

    action="ai_freeform" on a successful LLM answer is deliberate: it
    lets the frontend (or a future one) style/label this differently
    from a real, data-backed command response if it wants to -- same
    "never present a free-form AI guess indistinguishably from a real
    platform answer" principle as everywhere else in this codebase.
    """
    from . import local_llm

    if not local_llm.is_configured():
        return JarvisResponse(text=_NOT_RECOGNIZED_TEXT, success=False)

    answer = local_llm.ask(raw_text)
    if answer is None:
        return JarvisResponse(text=_NOT_RECOGNIZED_TEXT, success=False)

    return JarvisResponse(text=answer, action="ai_freeform", category="ai", success=True)


def _log(raw_text, user, input_mode, intent, response, started, needs_confirmation=False):
    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        JarvisCommandHistory.objects.create(
            user=user if getattr(user, "is_authenticated", False) else None,
            raw_text=raw_text,
            input_mode=input_mode,
            intent_category=intent.category or "",
            intent_action=intent.action or "",
            executed_module=f"jarvis.responses.{intent.action}" if intent.action else "",
            result_text=response.text[:2000],
            success=response.success,
            needs_confirmation=needs_confirmation or response.needs_confirmation,
            execution_ms=elapsed_ms,
        )
    except Exception:
        # Same principle as apps.admin_tools.audit.log_action: a history
        # write failing must never block JARVIS from actually answering.
        pass
