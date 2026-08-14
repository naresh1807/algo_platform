"""
Response Generator (manual 14.23 priority #5) + the actual handlers
behind every command category in commands.py.

Every function here does one of two things and says which honestly:
  1. Reads REAL data from another app (risk.engine, execution,
     signals, learning, analytics, market_data, news) and reports it --
     no function in this file invents a number.
  2. For something not yet wired up (e.g. a symbol with no candles
     ingested yet), says so in plain English rather than returning a
     fabricated value -- same "reporting nothing beats reporting a
     fabricated number" principle apps/analytics/services.py already
     uses for max_drawdown/slippage.

Each handler returns a plain string (manual 14.18's response style --
short, direct, explains what happened). engine.py is what turns a
handler's return value into the full structured API response.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from apps.execution.models import OpenPosition
from apps.learning.models import DailyReviewNote, DriftEvent, ModelRegistry, StrategyVersion
from apps.market_data.models import HistoricalData
from apps.monitoring.models import FeedHealthCheck
from apps.risk import engine as risk_engine
from apps.risk.models import RiskEvent
from apps.signals.models import TradingSignal


def _fmt(value, places=2) -> str:
    if value is None:
        return "not available yet"
    try:
        return f"{Decimal(str(value)):,.{places}f}"
    except (InvalidOperation, TypeError):
        return str(value)


# -------------------------------------------------------------- navigation
#
# manual 14.18: "Open Portfolio" -> "Portfolio opened successfully."
# Every navigation action gets this same shape of confirmation; the
# actual page change happens client-side in JarvisPanel.jsx, which
# reads the `route` field the engine attaches separately (see
# navigation.py) -- this text is only ever the spoken/displayed
# confirmation, never the navigation mechanism itself.

def _nav_confirmation(label: str):
    def _handler(entities: dict) -> str:
        return f"{label} opened successfully."
    return _handler


open_dashboard = _nav_confirmation("Dashboard")
open_charts = _nav_confirmation("Charts")
open_portfolio = _nav_confirmation("Portfolio")
open_risk = _nav_confirmation("Risk log")
open_settings = _nav_confirmation("Settings")
open_ai_dashboard = _nav_confirmation("AI dashboard")
open_paper_trading = _nav_confirmation("Paper trading")
open_watchlist = _nav_confirmation("Watchlist")
open_signals = _nav_confirmation("Signals")
open_options = _nav_confirmation("Options analytics")


def go_back(entities: dict) -> str:
    return "Going back."


# ---------------------------------------------------------------- market

def show_symbol(entities: dict) -> str:
    symbol = entities.get("symbol")
    if not symbol:
        return "Which symbol? Try \"show NIFTY\" or another symbol from your watchlist."
    candle = HistoricalData.objects.filter(symbol=symbol).order_by("-timestamp").first()
    if not candle:
        return f"No candles ingested yet for {symbol} -- the market_data ingestion task hasn't run for it."
    return (
        f"{symbol}: last close {_fmt(candle.close)} at {timezone.localtime(candle.timestamp):%H:%M}, "
        f"O {_fmt(candle.open)} H {_fmt(candle.high)} L {_fmt(candle.low)} V {candle.volume}."
    )


current_price = show_symbol  # 14.9 "Current Price" is the same lookup as "Show NIFTY"


def market_status(entities: dict) -> str:
    latest = FeedHealthCheck.objects.order_by("-checked_at").first()
    if not latest:
        return "No feed-health check has run yet -- can't tell you if the feed is live."
    state = "healthy" if latest.is_healthy else "DOWN"
    return f"Feed status ({latest.source}): {state}, last checked {timezone.localtime(latest.checked_at):%H:%M:%S}."


def todays_high(entities: dict) -> str:
    return _today_ohlc(entities.get("symbol"), "high")


def todays_low(entities: dict) -> str:
    return _today_ohlc(entities.get("symbol"), "low")


def _today_ohlc(symbol: str | None, field: str) -> str:
    if not symbol:
        return "Which symbol? Try \"today's high NIFTY\"."
    today = timezone.localdate()
    qs = HistoricalData.objects.filter(symbol=symbol, timestamp__date=today)
    if not qs.exists():
        return f"No candles for {symbol} today yet."
    candle = qs.order_by("-high").first() if field == "high" else qs.order_by("low").first()
    return f"Today's {field} for {symbol}: {_fmt(getattr(candle, field))}."


def market_summary(entities: dict) -> str:
    from apps.news.impact_engine import generate_market_summary

    summary = generate_market_summary(timezone.localdate())
    if not summary:
        return "No market summary available yet -- news polling may not have run today."
    return str(summary)[:600]


def best_strike(entities: dict) -> str:
    """
    manual intro (Problem 7): "AI analyzes Probability, OI, Greeks,
    Volume, Risk and suggests the Best Strike." Defaults to NIFTY /
    nearest synced expiry / bullish when the utterance doesn't specify
    -- same "reasonable default, state the assumption" pattern
    show_symbol uses for a missing symbol, except here a default is
    possible (symbol) while direction genuinely isn't guessable, so a
    bearish request needs "put"/"bearish"/"sell" in the phrase.
    """
    from apps.options.signals_engine import nearest_expiry
    from apps.options.strike_selector import suggest_best_strike

    underlying = entities.get("symbol") or "NIFTY"
    text = (entities.get("raw_text") or "").lower()
    direction = "bearish" if any(w in text for w in ("put", "bearish", "sell", "short")) else "bullish"

    expiry = nearest_expiry(underlying)
    if expiry is None:
        return f"No options data synced for {underlying} yet -- run sync_option_contracts first."

    result = suggest_best_strike(underlying, expiry, direction)
    if result["suggested"] is None:
        return f"{underlying} {direction} ({expiry}): {result['reason']}"
    return f"{underlying} {direction} ({expiry}): {result['reason']}"


def create_price_alert(entities: dict) -> str:
    """
    "Alert me when NIFTY crosses above 25000" -- symbol and price come
    from intent.py's entity extraction; direction comes from the
    utterance's own wording when present ("above"/"over"/"crosses
    above" vs "below"/"under"/"drops below"), and falls back to
    comparing the target price against the latest real close when the
    wording doesn't say (matching best_strike's "state the assumption"
    pattern for an ungiven parameter, applied to direction here instead
    of underlying).
    """
    from apps.market_data.models import HistoricalData
    from apps.monitoring.models import PriceAlert

    symbol = entities.get("symbol")
    price = entities.get("price")
    user = entities.get("_user")

    if not symbol:
        return "Which symbol? Try \"alert me when NIFTY above 25000\"."
    if price is None:
        return f"What price should I watch {symbol} for? Try \"alert me when {symbol} above 25000\"."
    if user is None or not getattr(user, "is_authenticated", False):
        return "You need to be signed in to create a price alert."

    text = entities.get("raw_text") or ""
    if any(w in text for w in ("below", "under", "drops")):
        condition = PriceAlert.Condition.BELOW
    elif any(w in text for w in ("above", "over", "crosses")):
        condition = PriceAlert.Condition.ABOVE
    else:
        latest = HistoricalData.objects.filter(symbol=symbol).order_by("-timestamp").first()
        if latest is None:
            return f"No price data for {symbol} yet, and the phrase didn't say above/below -- try being explicit."
        condition = PriceAlert.Condition.ABOVE if price >= float(latest.close) else PriceAlert.Condition.BELOW

    alert = PriceAlert.objects.create(
        symbol=symbol, condition=condition, target_price=price, created_by=user,
    )
    direction_word = "above" if condition == PriceAlert.Condition.ABOVE else "below"
    return f"Alert set: I'll tell you when {symbol} goes {direction_word} {alert.target_price}."


def list_price_alerts(entities: dict) -> str:
    from apps.monitoring.models import PriceAlert

    user = entities.get("_user")
    if user is None or not getattr(user, "is_authenticated", False):
        return "You need to be signed in to see your alerts."

    active = PriceAlert.objects.filter(created_by=user, active=True, triggered_at__isnull=True)
    if not active:
        return "No active price alerts. Try \"alert me when NIFTY above 25000\" to set one."
    lines = [f"{a.symbol} {a.condition} {a.target_price}" for a in active[:10]]
    return f"{active.count()} active alert(s): " + "; ".join(lines)


def stock_suggestions(entities: dict) -> str:
    """
    apps.investing -- long-term equity suggestions, a completely
    different question from every other JARVIS command in this file
    ("is this worth owning for years" vs "what's the intraday signal
    right now"). Reports the strongest current StockRecommendation
    rows (apps.investing.tasks.generate_stock_recommendations' own
    weekly output) rather than scoring anything live -- same "report
    the system's actual last output, don't recompute live" pattern
    best_strategy uses for apps.learning.ranking.
    """
    from apps.investing.models import StockRecommendation

    top = (
        StockRecommendation.objects.filter(verdict__in=["strong_hold", "moderate_hold"])
        .select_related("stock").order_by("-fundamental_score")[:5]
    )
    if not top:
        return (
            "No stock recommendations yet -- add stocks to your watchlist first "
            "(\"add TCS to my stock watchlist\"), fundamentals get pulled weekly."
        )
    lines = [
        f"{r.stock.symbol} ({r.fundamental_score:.0f}/100, {r.verdict}, "
        f"price {r.valuation_label or 'n/a'}, trend {r.technical_trend or 'n/a'}, ~{r.suggested_hold_months}mo)"
        for r in top
    ]
    return "Top current stock suggestions: " + "; ".join(lines) + ". Not investment advice -- verify before acting."


def stock_watchlist(entities: dict) -> str:
    from apps.investing.models import StockWatchlist

    user = entities.get("_user")
    if user is None or not getattr(user, "is_authenticated", False):
        return "You need to be signed in to see your stock watchlist."

    entries = StockWatchlist.objects.filter(user=user).select_related("stock")
    if not entries:
        return "Your stock watchlist is empty. Try \"add TCS to my stock watchlist\"."
    return f"{entries.count()} stock(s): " + ", ".join(e.stock.symbol for e in entries[:15])


def add_to_stock_watchlist(entities: dict) -> str:
    """
    "Add TCS to my stock watchlist" -- the symbol here is NOT one of
    intent.py's known-symbol entities (those only cover
    settings.WATCHLIST's small intraday index list); a long-term stock
    watchlist is open-ended over the whole exchange, so the symbol is
    pulled directly from the utterance's own "add <SYMBOL> to" shape
    instead, right here rather than teaching intent.py's generic
    entity extractor about every NSE ticker that exists.
    """
    import re

    from apps.investing.models import Stock, StockWatchlist

    user = entities.get("_user")
    if user is None or not getattr(user, "is_authenticated", False):
        return "You need to be signed in to add to your stock watchlist."

    text = entities.get("raw_text") or ""
    match = re.search(r"add (\w+) to", text)
    if not match:
        return "Which stock? Try \"add TCS to my stock watchlist\"."

    symbol = match.group(1).upper()
    stock, _ = Stock.objects.get_or_create(symbol=symbol, defaults={"name": symbol})
    _, created = StockWatchlist.objects.get_or_create(user=user, stock=stock)
    if not created:
        return f"{symbol} is already on your stock watchlist."
    return f"Added {symbol} to your stock watchlist. Fundamentals get pulled and scored on the weekly sync."


def ipo_suggestions(entities: dict) -> str:
    """
    manual has nothing on IPOs at all -- the trader asked for this
    directly. Reports real synced IPOListing rows
    (apps.investing.tasks.sync_ipo_calendar); no scoring/ranking logic
    exists for IPOs yet (a company's first-ever listing has no
    FundamentalSnapshot history to score against) -- this lists what's
    open/upcoming, it does not tell you which one is "best," and says
    so rather than implying a judgment it can't actually make.
    """
    from apps.investing.models import IPOListing

    active = IPOListing.objects.filter(status__in=["open", "upcoming"]).order_by("open_date")[:5]
    if not active:
        return "No upcoming or open IPOs synced yet."
    lines = [f"{i.company_name} ({i.status}, opens {i.open_date or 'TBA'})" for i in active]
    return "IPOs: " + "; ".join(lines) + ". No fundamentals history exists yet for a new listing, so this is a list, not a ranking."


def sector_analysis(entities: dict) -> str:
    """
    "సెక్టార్ వైస్‌గా వెరిఫై చేయగలగాలి, అక్కడ వచ్చిన ప్రాఫిటబుల్ స్టాక్‌ని
    సజెస్ట్ చేయాలి" -- sector-wise verification + the best stock in
    each sector. See apps.investing.sector_analysis for the actual
    grouping/aggregation.
    """
    from apps.investing.sector_analysis import sector_breakdown

    sectors = sector_breakdown()
    if not sectors:
        return "No sector data yet -- add stocks to your watchlist and wait for the weekly fundamentals sync."

    lines = []
    for s in sectors[:5]:
        if s["best_stock"]:
            lines.append(
                f"{s['sector']} (avg {s['avg_score']:.0f}/100, {s['stock_count']} stock(s)): "
                f"best is {s['best_stock']['symbol']} ({s['best_stock']['score']:.0f}/100, {s['best_stock']['verdict']})"
            )
        else:
            lines.append(f"{s['sector']} (avg {s['avg_score']:.0f}/100, only {s['stock_count']} stock tracked -- too few for a real comparison)")
    return "Sector breakdown: " + " | ".join(lines)


def market_outlook(entities: dict) -> str:
    """
    "ఇది ఎప్పటికప్పుడు అన్ని స్టాక్స్ ఆప్షన్స్ ఇండెక్స్ పైన మరియు
    న్యూస్ పైన అబ్జర్వ్ చేసి మార్కెట్ ఏ విధంగా ఉండబోతుందో... విశ్లేషించి
    సరైన సమాధానం చెప్పి" -- the single synthesized report. Reports the
    latest stored MarketOutlookSnapshot (apps.jarvis.tasks.
    generate_market_outlook's own periodic output) rather than
    recomputing live on every JARVIS question -- same "report the
    system's actual last output" pattern every other JARVIS command in
    this file uses (best_strategy, sector_analysis, etc.). If nothing
    has been generated yet (fresh install, or outside the periodic
    task's market-hours schedule), computes one on the spot instead of
    saying "no data" -- this one case benefits from always having an
    answer ready, unlike a stored history table.
    """
    from .models import MarketOutlookSnapshot

    latest = MarketOutlookSnapshot.objects.order_by("-generated_at").first()
    if latest is not None:
        return latest.summary

    from .market_intelligence import market_outlook as compute_market_outlook
    return compute_market_outlook()["summary"]


# --------------------------------------------------------------- portfolio

def _equity_snapshot() -> str | None:
    try:
        return risk_engine.get_equity()
    except Exception:
        return None


def current_profit(entities: dict) -> str:
    equity = _equity_snapshot()
    if equity is None:
        return "No equity record yet -- run seed_demo_data or let paper trading start."
    pnl_pct = equity.daily_pnl_pct
    direction = "profit" if pnl_pct >= 0 else "loss"
    return f"Today's {direction}: {abs(pnl_pct):.2f}% (equity {_fmt(equity.current_equity)})."


current_loss = current_profit  # same underlying number, framed by sign


def available_capital(entities: dict) -> str:
    equity = _equity_snapshot()
    if equity is None:
        return "No equity record yet."
    return f"Current equity: {_fmt(equity.current_equity)}. Peak equity: {_fmt(equity.peak_equity)}."


def open_trades(entities: dict) -> str:
    positions = OpenPosition.objects.filter(closed_at__isnull=True)
    count = positions.count()
    if count == 0:
        return "No open positions right now."
    lines = [f"{p.symbol} {p.side} x{p.qty} @ {_fmt(p.entry_price)} (P&L {_fmt(p.unrealized_pnl)})" for p in positions[:5]]
    more = f" (+{count - 5} more)" if count > 5 else ""
    return f"{count} open position(s): " + "; ".join(lines) + more


def closed_trades(entities: dict) -> str:
    closed = OpenPosition.objects.filter(closed_at__isnull=False).order_by("-closed_at")[:5]
    if not closed:
        return "No closed trades yet."
    lines = [f"{p.symbol} closed {timezone.localtime(p.closed_at):%b %d} P&L {_fmt(p.unrealized_pnl)}" for p in closed]
    return "Recent closed trades: " + "; ".join(lines)


def roi(entities: dict) -> str:
    equity = _equity_snapshot()
    if equity is None:
        return "No equity record yet."
    if equity.daily_start_equity:
        pct = float((equity.current_equity - equity.daily_start_equity) / equity.daily_start_equity * 100)
        return f"ROI today: {pct:.2f}%."
    return "Can't compute ROI -- daily_start_equity is zero."


def todays_performance(entities: dict) -> str:
    from apps.analytics.models import PerformanceMetrics

    metrics = PerformanceMetrics.objects.filter(date=timezone.localdate()).first()
    if not metrics:
        return "Today's performance hasn't been computed yet -- run compute_daily_performance."
    return (
        f"Trades: {metrics.total_trades}, win rate: {_pct(metrics.win_rate)}, "
        f"profit factor: {_fmt(metrics.profit_factor)}, expectancy: {_fmt(metrics.expectancy)}R."
    )


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


# ----------------------------------------------------------- paper trading

def start_paper_trading(entities: dict) -> str:
    return (
        "Paper trading runs on the Celery beat schedule (run_trading_cycle), not a switch JARVIS "
        "flips. If Celery worker + beat aren't running, start them: "
        "`celery -A config worker -l info` and `celery -A config beat -l info`."
    )


def stop_paper_trading(entities: dict) -> str:
    return (
        "JARVIS can't stop the Celery beat schedule from here (manual 14.21 -- JARVIS doesn't control "
        "process lifecycle). Stop the celery beat process directly, or set BROKER_MODE appropriately."
    )


def trade_history(entities: dict) -> str:
    return closed_trades(entities)


# ------------------------------------------------------------------- ai

def ai_status(entities: dict) -> str:
    active = ModelRegistry.objects.filter(active_flag=True).order_by("-created_at").first()
    if not active:
        return "No ML model trained yet -- still cold-start. See apps.learning.ml_train.MIN_TRAINING_SAMPLES."
    metrics = active.metrics_json or {}
    return f"Active model: {active.model_name} v{active.model_version}, accuracy {_pct(metrics.get('accuracy'))}."


def learning_progress(entities: dict) -> str:
    count = ModelRegistry.objects.count()
    closed = OpenPosition.objects.filter(closed_at__isnull=False).count()
    return f"{closed} closed trades so far; {count} model version(s) trained from them."


def todays_accuracy(entities: dict) -> str:
    return ai_status(entities)


def confidence_report(entities: dict) -> str:
    recent = TradingSignal.objects.exclude(ml_win_probability__isnull=True).order_by("-created_at")[:5]
    if not recent:
        return "No signals have an ML win-probability yet."
    from apps.learning.confidence import confidence_band

    lines = [f"{s.symbol} {s.signal_type} -> {_pct(s.ml_win_probability)} ({confidence_band(s.ml_win_probability)})" for s in recent]
    return "Recent ML win-probabilities: " + "; ".join(lines)


def current_confidence(entities: dict) -> str:
    """manual 13.10/13.17 "Current Confidence" -- the band for the most recent signal."""
    from apps.learning.confidence import confidence_band

    last = TradingSignal.objects.order_by("-created_at").first()
    if not last:
        return "No signals generated yet."
    return (
        f"{last.symbol} {last.signal_type}: confidence {_pct(last.ml_win_probability)} "
        f"({confidence_band(last.ml_win_probability)})."
    )


def best_strategy(entities: dict) -> str:
    from apps.learning.ranking import rank_strategies

    ranked = rank_strategies()
    if not ranked:
        return "No StrategyVersion has any closed trades yet -- nothing to rank."
    top = ranked[0]
    marker = " (currently active)" if top["active"] else " (not currently active)"
    lines = [
        f"Best: {top['version_name']}{marker} -- win rate {top['win_rate']:.0%}, "
        f"profit factor {top['profit_factor']:.2f}, {top['trade_count']} trade(s)."
    ]
    if len(ranked) > 1:
        rest = "; ".join(
            f"{r['version_name']} ({r['win_rate']:.0%} WR, {r['trade_count']} trades)"
            for r in ranked[1:4]
        )
        lines.append(f"Others: {rest}.")
    return " ".join(lines)


def explain_last_decision(entities: dict) -> str:
    last = TradingSignal.objects.order_by("-created_at").first()
    if not last:
        return "No signals generated yet."
    return f"{last.symbol} {last.signal_type} ({last.status}): {last.reason[:300]}"


def stop_ai_learning(entities: dict) -> str:
    return "RESTRICTED"  # engine.py intercepts restricted actions before calling handlers


# ------------------------------------------------------------------ risk

def show_risk(entities: dict) -> str:
    equity = _equity_snapshot()
    kill_active = risk_engine.is_kill_switch_active()
    if equity is None:
        return f"Kill switch: {'ACTIVE' if kill_active else 'inactive'}. No equity record yet."
    return (
        f"Kill switch: {'ACTIVE' if kill_active else 'inactive'}. "
        f"Drawdown: {equity.drawdown_pct:.2f}%. Consecutive losses: {equity.consecutive_losses}."
    )


def show_drawdown(entities: dict) -> str:
    equity = _equity_snapshot()
    if equity is None:
        return "No equity record yet."
    return f"Current drawdown: {equity.drawdown_pct:.2f}% (peak {_fmt(equity.peak_equity)}, current {_fmt(equity.current_equity)})."


def daily_loss(entities: dict) -> str:
    equity = _equity_snapshot()
    if equity is None:
        return "No equity record yet."
    pct = equity.daily_pnl_pct
    return f"Today's P&L: {pct:.2f}%."


def kill_switch_status(entities: dict) -> str:
    active = risk_engine.is_kill_switch_active()
    if not active:
        return "Kill switch is inactive. Trading is allowed (subject to the other risk checks)."
    return (
        "Kill switch is ACTIVE -- no new trades will be approved. It can only be re-armed manually "
        "via `python manage.py rearm_kill_switch --by \"you\" --confirm` -- JARVIS cannot do this "
        "(manual 14.21)."
    )


def emergency_status(entities: dict) -> str:
    critical = RiskEvent.objects.filter(severity="critical").order_by("-created_at").first()
    if not critical:
        return "No critical risk events on record."
    return f"Most recent critical event: {critical.event_type} -- {critical.message[:200]} ({timezone.localtime(critical.created_at):%b %d %H:%M})."


def risk_report(entities: dict) -> str:
    recent = RiskEvent.objects.order_by("-created_at")[:5]
    if not recent:
        return "No risk events logged yet."
    lines = [f"[{e.severity}] {e.event_type} ({timezone.localtime(e.created_at):%H:%M})" for e in recent]
    return "Recent risk events: " + "; ".join(lines)


def sharpe_ratio(entities: dict) -> str:
    from apps.analytics.services import compute_sharpe_ratio

    value = compute_sharpe_ratio(lookback_days=30)
    if value is None:
        return "Not enough equity history yet (need at least 3 distinct trading days) to compute a Sharpe ratio."
    return f"30-day Sharpe ratio: {value:.2f}."


# -------------------------------------------------------------- strategy

def current_strategy(entities: dict) -> str:
    """
    "current strategy" / "active strategy" -- must report the
    StrategyVersion actually flagged active_flag=True, NOT the
    top-ranked one from best_strategy() (a different command, phrase
    "best strategy", AI category): the best-performing version and the
    currently active one are frequently different versions, and
    aliasing this to best_strategy() used to produce answers like
    "Best: v3 (not currently active)" in response to "what's my
    active strategy" -- self-contradictory. See strategy_performance()
    below for the same active_flag lookup with signal-count framing.
    """
    from apps.learning.ranking import rank_strategies

    active = StrategyVersion.objects.filter(active_flag=True).first()
    if not active:
        return "No active strategy version."
    stats = next((r for r in rank_strategies() if r["version_name"] == active.version_name), None)
    if stats is None:
        return f"Active strategy: {active.version_name} -- no closed trades yet to report win rate/profit factor."
    return (
        f"Active strategy: {stats['version_name']} -- win rate {stats['win_rate']:.0%}, "
        f"profit factor {stats['profit_factor']:.2f}, {stats['trade_count']} trade(s)."
    )


def strategy_performance(entities: dict) -> str:
    active = StrategyVersion.objects.filter(active_flag=True).first()
    if not active:
        return "No active strategy version."
    signal_count = TradingSignal.objects.filter(strategy_version=active).count()
    return f"{active.version_name} has produced {signal_count} signal(s) so far."


# ------------------------------------------------------------- automation

def morning_checklist(entities: dict) -> str:
    from .automation import morning_checklist as run_checklist

    return run_checklist()


def market_open_routine(entities: dict) -> str:
    from .automation import market_open_routine as run_routine

    return run_routine()


def daily_report(entities: dict) -> str:
    from .automation import daily_report as run_report

    return run_report()


def system_health_check(entities: dict) -> str:
    from .automation import system_health_check as run_check

    return run_check()


def export_trades(entities: dict) -> str:
    from apps.admin_tools.exports import export_closed_trades_csv

    path, count = export_closed_trades_csv()
    if count == 0:
        return "No closed trades to export yet."
    return f"Exported {count} closed trade(s) to {path}."


def backup_database(entities: dict) -> str:
    from apps.admin_tools.tasks import run_database_backup

    # Runs via Celery, not inline -- mysqldump on a real database can
    # take a while, and this command is called from a request/response
    # cycle. JARVIS will announce completion separately (manual 14.16)
    # once the task finishes, over the same /ws/jarvis/live/ feed as
    # every other announcement -- see apps/jarvis/signals.py and
    # apps/admin_tools/management/commands/backup_database.py.
    try:
        run_database_backup.delay()
    except Exception as exc:
        return (
            f"Couldn't queue the backup task: {exc}. Celery worker may not be running -- "
            f"you can also run `python manage.py backup_database` directly."
        )
    return "Database backup started. JARVIS will announce here when it's done."


def delete_history(entities: dict) -> str:
    return "RESTRICTED"


def reset_portfolio(entities: dict) -> str:
    return "RESTRICTED"


def exit_all_positions(entities: dict) -> str:
    return "RESTRICTED"
