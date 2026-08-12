"""
The command registry (manual 14.23 priority #1: Command Engine).

This is the single source of truth for "what can JARVIS actually do" --
categories mirror the manual's own sections 14.8 through 14.15 exactly,
so the dashboard's "Suggested Commands" panel (14.19) and the intent
parser (intent.py) both read from here rather than keeping their own
separate lists that could drift apart.

Each entry maps one canonical `action` to:
  - category: which manual section it belongs to
  - phrases: example phrasings intent.py's keyword matcher looks for
    (deliberately simple substring/keyword matching, not a trained NLU
    model -- see intent.py's module docstring for why that's the
    honest starting point here)
  - restricted: True if manual 14.21 requires explicit confirmation
    before this action executes
"""

from __future__ import annotations

NAVIGATION = "navigation"
MARKET = "market"
PORTFOLIO = "portfolio"
PAPER_TRADING = "paper_trading"
AI = "ai"
RISK = "risk"
STRATEGY = "strategy"
AUTOMATION = "automation"

CATEGORIES = [NAVIGATION, MARKET, PORTFOLIO, PAPER_TRADING, AI, RISK, STRATEGY, AUTOMATION]

# manual 14.21: JARVIS must always ask confirmation for these, even
# though the underlying action itself (e.g. closing a position) is
# something the platform can already do -- the restriction is on JARVIS
# executing it unprompted, not on the action existing.
RESTRICTED_ACTIONS = {
    "reset_portfolio",
    "delete_history",
    "stop_ai_learning",
    "exit_all_positions",
}

# manual 14.21: these are hard-refused, not confirmation-gated -- no
# confirmation flag can ever make JARVIS do them, because they'd bypass
# apps.risk's own governance (kill-switch re-arm is deliberately a
# management-command-only action per apps/risk/management/commands/
# rearm_kill_switch.py; changing RISK_HARD_LIMITS or placing a live
# order are not exposed to JARVIS at all -- see engine.py).
#
# NONE of these four names are ever registered as keys in COMMANDS
# below -- so intent.detect_intent() can never actually return one from
# real user input, and engine.py's FORBIDDEN_ACTIONS check is presently
# unreachable defense-in-depth, not an enforcement point real traffic
# exercises today. That's intentional layering, not a mistake: it's
# what keeps this set safe to check even if a future edit ever
# accidentally added one of these names to COMMANDS (a copy-paste of an
# existing entry, a careless rename) -- the check here still stops it
# from being dispatchable, on top of it simply not being registered.
# apps/jarvis/tests.py's test_forbidden_action_never_executes guards
# this exact scenario.
FORBIDDEN_ACTIONS = {
    "change_risk_rules",
    "bypass_kill_switch",
    "rearm_kill_switch",
    "place_live_order",
}

COMMANDS = {
    # 14.8 Navigation Commands
    "open_dashboard": {"category": NAVIGATION, "phrases": ["open dashboard", "go home", "home"]},
    "open_charts": {"category": NAVIGATION, "phrases": ["open charts", "show charts"]},
    "open_portfolio": {"category": NAVIGATION, "phrases": ["open portfolio", "show portfolio"]},
    "open_risk": {"category": NAVIGATION, "phrases": ["open risk", "show risk log", "risk page"]},
    "open_settings": {"category": NAVIGATION, "phrases": ["open settings"]},
    "open_ai_dashboard": {"category": NAVIGATION, "phrases": ["open ai dashboard", "open learning", "ai page"]},
    "open_paper_trading": {"category": NAVIGATION, "phrases": ["open paper trading", "paper trading page"]},
    "open_watchlist": {"category": NAVIGATION, "phrases": ["open watchlist", "show watchlist"]},
    "open_signals": {"category": NAVIGATION, "phrases": ["open signals", "show signals"]},
    "open_options": {"category": NAVIGATION, "phrases": ["open options", "options analytics"]},
    "go_back": {"category": NAVIGATION, "phrases": ["go back", "back"]},

    # 14.9 Market Commands
    "show_symbol": {"category": MARKET, "phrases": ["show nifty", "show banknifty", "show "]},
    "current_price": {"category": MARKET, "phrases": ["current price", "price of", "ltp"]},
    "market_status": {"category": MARKET, "phrases": ["market status", "is market open"]},
    "todays_high": {"category": MARKET, "phrases": ["today's high", "todays high"]},
    "todays_low": {"category": MARKET, "phrases": ["today's low", "todays low"]},
    "market_summary": {
        "category": MARKET,
        "phrases": [
            "market summary", "summarize the market",
            # Added after a real reported gap: "what's today's important
            # news for indian markets" fell through to the not-recognized
            # fallback because none of the original phrases above are
            # substrings of it. This command already answers exactly
            # this (responses.market_summary wraps the same
            # apps.news.impact_engine.generate_market_summary used on
            # the Dashboard/News page) -- it just needed matching
            # phrases, not new logic.
            "today's news", "todays news", "important news", "news update",
            "what's the news", "whats the news", "news for indian markets",
            "market news", "latest news",
            # Second real gap, same root cause: "tell what news today
            # that impact indian stock market" still didn't match any
            # phrase above (word order/phrasing genuinely varies more
            # than a fixed phrase list can fully anticipate -- see
            # intent.py's own honest scope note on this). Adding the
            # specific substrings actually present in reported phrases,
            # same incremental approach as before, rather than trying to
            # guess every future variant up front.
            "impact indian stock market", "impact on indian stock market",
            "impact the indian stock market", "affect indian stock market",
            "affecting indian stock market", "stock market news",
            "news impact", "indian stock market news", "news today",
        ],
    },
    "best_strike": {"category": MARKET, "phrases": ["best strike", "suggest strike", "which strike"]},
    "create_price_alert": {"category": MARKET, "phrases": ["alert me when", "set alert", "notify me when", "create alert"]},
    "list_price_alerts": {"category": MARKET, "phrases": ["my alerts", "show alerts", "list alerts", "price alerts"]},
    "stock_suggestions": {"category": MARKET, "phrases": ["stock suggestions", "suggest stocks", "which stocks to buy", "stock recommendations"]},
    "stock_watchlist": {"category": MARKET, "phrases": ["stock watchlist", "my stocks", "watchlist stocks"]},
    "add_to_stock_watchlist": {"category": MARKET, "phrases": ["to my stock watchlist", "to stock watchlist", "to watchlist"]},
    "ipo_suggestions": {"category": MARKET, "phrases": ["ipo suggestions", "upcoming ipos", "suggest ipo", "ipo list"]},
    "sector_analysis": {"category": MARKET, "phrases": ["sector analysis", "best sector", "sector wise", "which sector"]},
    "market_outlook": {"category": MARKET, "phrases": ["market outlook", "market intelligence", "what should i buy", "what to buy today"]},

    # 14.10 Portfolio Commands
    "current_profit": {"category": PORTFOLIO, "phrases": ["current profit", "today's profit", "todays profit"]},
    "current_loss": {"category": PORTFOLIO, "phrases": ["current loss", "today's loss"]},
    "available_capital": {"category": PORTFOLIO, "phrases": ["available capital", "available cash"]},
    "open_trades": {"category": PORTFOLIO, "phrases": ["open trades", "open positions", "show open position"]},
    "closed_trades": {"category": PORTFOLIO, "phrases": ["closed trades", "trade history"]},
    "roi": {"category": PORTFOLIO, "phrases": ["roi", "return on investment"]},
    "todays_performance": {"category": PORTFOLIO, "phrases": ["today's performance", "todays performance"]},

    # 14.11 Paper Trading Commands (start/stop are automation-gated --
    # see automation.py; there is no separate on/off flag to flip, so
    # these report and reason about Celery-beat state honestly)
    "start_paper_trading": {"category": PAPER_TRADING, "phrases": ["start paper trading"]},
    "stop_paper_trading": {"category": PAPER_TRADING, "phrases": ["stop paper trading"]},
    "reset_portfolio": {"category": PAPER_TRADING, "phrases": ["reset portfolio"]},
    "exit_all_positions": {"category": PAPER_TRADING, "phrases": ["close all trades", "exit all positions"]},
    "trade_history": {"category": PAPER_TRADING, "phrases": ["trade history"]},

    # 14.12 AI Commands
    "ai_status": {"category": AI, "phrases": ["ai status"]},
    "learning_progress": {"category": AI, "phrases": ["learning progress"]},
    "todays_accuracy": {"category": AI, "phrases": ["today's accuracy", "todays accuracy"]},
    "confidence_report": {"category": AI, "phrases": ["confidence report"]},
    "current_confidence": {"category": AI, "phrases": ["current confidence"]},
    "best_strategy": {"category": AI, "phrases": ["best strategy"]},
    "explain_last_decision": {"category": AI, "phrases": ["explain last trade", "explain last decision", "why last trade"]},
    "stop_ai_learning": {"category": AI, "phrases": ["stop ai learning", "stop learning"]},

    # 14.13 Risk Commands
    "show_risk": {"category": RISK, "phrases": ["show risk"]},
    "show_drawdown": {"category": RISK, "phrases": ["show drawdown", "drawdown"]},
    "daily_loss": {"category": RISK, "phrases": ["daily loss"]},
    "kill_switch_status": {"category": RISK, "phrases": ["kill switch status", "kill switch"]},
    "emergency_status": {"category": RISK, "phrases": ["emergency status"]},
    "risk_report": {"category": RISK, "phrases": ["risk report"]},
    "sharpe_ratio": {"category": RISK, "phrases": ["sharpe ratio", "risk adjusted return"]},

    # 14.14 Strategy Commands (version 1.0: report-only -- manual never
    # gives JARVIS write access to StrategyVersion; that stays a human
    # admin action via the existing API, per common/permissions.py)
    "current_strategy": {"category": STRATEGY, "phrases": ["current strategy", "active strategy"]},
    "strategy_performance": {"category": STRATEGY, "phrases": ["strategy performance"]},

    # 14.15 Automation Commands
    "morning_checklist": {"category": AUTOMATION, "phrases": ["morning checklist"]},
    "market_open_routine": {"category": AUTOMATION, "phrases": ["market open routine", "start market open routine"]},
    "daily_report": {"category": AUTOMATION, "phrases": ["daily report"]},
    "system_health_check": {"category": AUTOMATION, "phrases": ["system health check", "system health", "health check"]},
    "export_trades": {"category": AUTOMATION, "phrases": ["export trades", "export trade history"]},
    "backup_database": {"category": AUTOMATION, "phrases": ["backup database", "backup the database"]},
    "delete_history": {"category": AUTOMATION, "phrases": ["delete history", "delete command history"]},
}


def suggested_commands(category: str | None = None) -> list[str]:
    """Powers the dashboard's Suggested Commands panel (manual 14.19)."""
    items = COMMANDS.items() if category is None else (
        (k, v) for k, v in COMMANDS.items() if v["category"] == category
    )
    return [v["phrases"][0] for _, v in items]
