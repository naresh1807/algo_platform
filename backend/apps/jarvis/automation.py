"""
Workflow Automation (manual 14.15, 14.20, 14.23 priority #7).

Honest scope: manual 14.20's "Market Open" workflow (Connect Broker ->
Start WebSocket -> Load Watchlist -> Start Indicators -> Enable
Strategies -> Ready) describes starting OS-level processes (Celery
worker, Celery beat, Daphne). A Django request handler cannot spawn or
supervise those processes -- they're either already running (started
by the operator per the README) or they aren't. So every function
below does the honest version of "automation": it CHECKS the real
state of each step and reports exactly which ones are ready and which
ones need a human to start something, rather than pretending to have
run a routine it has no ability to actually run.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.execution.models import OpenPosition
from apps.market_data.models import HistoricalData
from apps.monitoring.models import FeedHealthCheck
from apps.risk import engine as risk_engine
from apps.signals.models import TradingSignal


def morning_checklist() -> str:
    lines = []

    feed = FeedHealthCheck.objects.order_by("-checked_at").first()
    if feed and feed.is_healthy and feed.checked_at > timezone.now() - timedelta(minutes=15):
        lines.append("[OK] Feed healthy (checked recently)")
    else:
        lines.append("[!] Feed check missing or stale -- ingestion task may not be running")

    kill_active = risk_engine.is_kill_switch_active()
    lines.append("[!] Kill switch is ACTIVE" if kill_active else "[OK] Kill switch inactive")

    watchlist = ", ".join(getattr(settings, "WATCHLIST", []))
    lines.append(f"[i] Watchlist: {watchlist}")

    recent_candle = HistoricalData.objects.order_by("-timestamp").first()
    if recent_candle and recent_candle.timestamp > timezone.now() - timedelta(hours=1):
        lines.append("[OK] Recent candle data present")
    else:
        lines.append("[!] No recent candles -- check Celery beat is running ingest_watchlist_candles")

    return "Morning checklist:\n" + "\n".join(lines)


def market_open_routine() -> str:
    """Reports readiness against the manual 14.20 sequence, step by step."""
    steps = []

    broker_mode = getattr(settings, "BROKER_MODE", "paper")
    from apps.execution.models import get_execution_mode

    execution_mode = get_execution_mode()
    steps.append(f"1. Broker: BROKER_MODE={broker_mode} (data source), EXECUTION_MODE={execution_mode} (order placement)")

    feed = FeedHealthCheck.objects.order_by("-checked_at").first()
    feed_ok = bool(feed and feed.is_healthy)
    steps.append(f"2. Feed/WebSocket: {'OK' if feed_ok else 'NOT CONFIRMED -- no healthy FeedHealthCheck row'}")

    watchlist = getattr(settings, "WATCHLIST", [])
    steps.append(f"3. Watchlist loaded: {', '.join(watchlist) or 'EMPTY -- set WATCHLIST in .env'}")

    recent_candle = HistoricalData.objects.order_by("-timestamp").first()
    indicators_ready = bool(recent_candle and recent_candle.timestamp > timezone.now() - timedelta(hours=1))
    steps.append(f"4. Indicators/candles: {'fresh' if indicators_ready else 'stale or missing'}")

    recent_signal = TradingSignal.objects.order_by("-created_at").first()
    strategies_ready = bool(recent_signal and recent_signal.created_at > timezone.now() - timedelta(hours=1))
    steps.append(f"5. Signal engine: {'has run recently' if strategies_ready else 'no recent signal -- generate_signals_for_watchlist may not be scheduled'}")

    ready = feed_ok and bool(watchlist) and indicators_ready
    steps.append("READY" if ready else "NOT READY -- see [!] items above; this requires Celery worker + beat running, which JARVIS cannot start for you")

    return "Market-open routine check:\n" + "\n".join(steps)


def daily_report() -> str:
    from apps.analytics.models import PerformanceMetrics

    today = timezone.localdate()
    metrics = PerformanceMetrics.objects.filter(date=today).first()
    open_count = OpenPosition.objects.filter(closed_at__isnull=True).count()
    closed_today = OpenPosition.objects.filter(closed_at__date=today).count()

    if not metrics:
        return (
            f"No PerformanceMetrics computed for {today} yet -- run "
            f"`python manage.py run_backtest` or wait for the scheduled job. "
            f"({open_count} open, {closed_today} closed today.)"
        )
    return (
        f"Daily report {today}: {metrics.total_trades} trades, "
        f"win rate {'-' if metrics.win_rate is None else f'{metrics.win_rate * 100:.1f}%'}, "
        f"{open_count} currently open, {closed_today} closed today."
    )


def system_health_check() -> str:
    lines = []
    feed = FeedHealthCheck.objects.order_by("-checked_at").first()
    lines.append(f"Feed: {'healthy' if feed and feed.is_healthy else 'unhealthy or unknown'}"
                 + (f" (checked {timezone.localtime(feed.checked_at):%H:%M})" if feed else ""))
    lines.append(f"Kill switch: {'ACTIVE' if risk_engine.is_kill_switch_active() else 'inactive'}")
    equity = None
    try:
        equity = risk_engine.get_equity()
    except Exception:
        pass
    lines.append(f"Equity record: {'present' if equity else 'missing'}")
    last_signal = TradingSignal.objects.order_by("-created_at").first()
    lines.append(
        "Last signal: " + (
            f"{timezone.localtime(last_signal.created_at):%H:%M} ({last_signal.symbol})"
            if last_signal else "none yet"
        )
    )
    return "System health:\n" + "\n".join(lines)
