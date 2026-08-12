"""
Navigation Module (manual 14.23 priority #3).

JARVIS runs on the Django backend; the frontend router (React Router,
see frontend/src/App.jsx) is what actually changes what's on screen.
So "navigation" here means one thing: mapping a navigation action to
the frontend route it corresponds to, and returning that route in the
command response. JarvisPanel.jsx is responsible for calling
navigate(route) when it sees one -- this module never touches the DOM
or holds any UI state itself, matching the manual's "AI must explain
every signal" -> here, "JARVIS must explain every navigation" pattern:
the response always says which page it opened, not just silently
returns a path.
"""

from __future__ import annotations

ROUTES: dict[str, str] = {
    "open_dashboard": "/",
    "open_charts": "/",
    "open_signals": "/signals",
    "open_options": "/options",
    "open_portfolio": "/positions",
    "open_watchlist": "/",
    "open_positions": "/positions",
    "open_risk": "/risk",
    "open_ai_dashboard": "/learning",
    "open_paper_trading": "/positions",
    "open_settings": "/",
    "go_back": "__back__",
}


def route_for(action: str) -> str | None:
    return ROUTES.get(action)
