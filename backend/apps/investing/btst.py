"""Read-only equity BTST signal scan. Never places orders."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from celery import shared_task
from django.utils import timezone
from apps.market_data.market_hours import NSE_HOLIDAYS, is_market_open
from .models import Stock, BTSTRun, BTSTSignal

IST = ZoneInfo("Asia/Kolkata")


def next_session(day):
    for offset in range(1, 15):
        candidate = day + timedelta(days=offset)
        if candidate.year != 2026:
            raise ValueError("Verified BTST exchange calendar is required for the next year.")
        if candidate.weekday() < 5 and candidate not in NSE_HOLIDAYS:
            return candidate
    raise ValueError("No next session found.")


STRATEGIES = {
    "trend_breakout": "20-day trend breakout",
    "inside_day_breakout": "Inside-day breakout",
    "ema_pullback": "EMA20 pullback recovery",
    "opening_range_vwap": "Opening-range breakout above VWAP",
}


def evaluate_setup(history, intraday):
    """Evaluate completed, chronological bars supplied by the cutoff-safe scanner."""
    if len(history) < 50 or len(intraday) < 60:
        return None, "insufficient daily or intraday history"
    columns = ["open", "high", "low", "close", "volume"]
    try:
        daily = pd.DataFrame(history)[columns].astype(float)
        day = pd.DataFrame(intraday)[columns].astype(float)
    except (KeyError, ValueError, TypeError):
        return None, "invalid price or volume inputs"
    for frame in (daily, day):
        if (not np.isfinite(frame.to_numpy()).all() or (frame[columns[:4]] <= 0).any().any()
                or (frame.volume < 0).any() or (frame.high < frame.low).any()
                or (frame.high < frame[["open", "close"]].max(axis=1)).any()
                or (frame.low > frame[["open", "close"]].min(axis=1)).any()):
            return None, "invalid price or volume inputs"
    close = float(day.iloc[-1].close)
    ema20 = float(daily.close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(daily.close.ewm(span=50, adjust=False).mean().iloc[-1])
    breakout = float(daily.high.tail(20).max())
    previous = daily.close.shift(1)
    true_range = pd.concat([daily.high-daily.low, (daily.high-previous).abs(),
                            (daily.low-previous).abs()], axis=1).max(axis=1)
    atr = float(true_range.tail(14).mean())
    average_volume = float(daily.volume.tail(20).mean())
    if atr <= 0 or average_volume <= 0:
        return None, "invalid price or volume inputs"
    volume_ratio = float(day.volume.sum()) / (average_volume * 315 / 375)
    session_low = float(day.low.min())
    span = float(day.high.max()) - session_low
    strength = (close-session_low)/span if span > 0 else 0
    total_volume = float(day.volume.sum())
    vwap = float((((day.high+day.low+day.close)/3)*day.volume).sum()/total_volume) if total_volume > 0 else 0
    opening_high = float(day.high.iloc[:30].max())
    inside_day = bool(daily.iloc[-1].high < daily.iloc[-2].high and daily.iloc[-1].low > daily.iloc[-2].low)
    trend = close > ema20 > ema50
    # Common liquidity and closing-strength filters apply to every setup.
    if not (trend and volume_ratio >= 1.2 and strength >= .8):
        return None, "trend, volume or closing-strength conditions not met"
    matches = []
    if close > breakout:
        matches.append("trend_breakout")
    if inside_day and close > float(daily.iloc[-2].high):
        matches.append("inside_day_breakout")
    if (ema20-0.5*atr <= session_low <= ema20+0.25*atr
            and close > float(daily.iloc[-1].high) and close > float(day.iloc[0].open)):
        matches.append("ema_pullback")
    if close > opening_high and close > vwap and close > float(daily.iloc[-1].high):
        matches.append("opening_range_vwap")
    if not matches:
        return None, "no BTST strategy conditions met"
    stop = max(session_low, close-atr)
    risk = close-stop
    if risk <= 0 or risk/close > .03:
        return None, "stop distance exceeds 3 percent or is invalid"
    return {"entry_reference": round(close, 2), "stop_loss": round(stop, 2),
            "target_price": round(close+1.5*risk, 2),
            "evidence": {"strategy": matches[0], "strategies": matches,
                         "ema20": ema20, "ema50": ema50, "breakout_20d": breakout,
                         "volume_ratio": volume_ratio, "intraday_strength": strength,
                         "atr14": atr, "risk_pct": risk/close*100, "vwap": vwap,
                         "opening_range_high": opening_high, "inside_day": inside_day}}, "qualified"


@shared_task(name="apps.investing.btst.generate_btst_signals", expires=600)
def generate_btst_signals():
    from apps.market_data.broker_client import get_broker_client
    from apps.options.instrument_master import get_instrument_master
    now=timezone.now(); local=now.astimezone(IST); day=local.date()
    if day.year!=2026:
        return {"status":"skipped","reason":"verified calendar missing for year"}
    is_open, reason=is_market_open(now)
    if not is_open or not time(14,30)<=local.time().replace(tzinfo=None)<time(14,40):
        return {"status":"skipped","reason":reason or "outside 14:30-14:40 IST scan window"}
    cutoff=datetime.combine(day,time(14,30),tzinfo=IST)
    run,created=BTSTRun.objects.get_or_create(trading_date=day,defaults={"scheduled_for":cutoff})
    if not created:
        return {"status":"duplicate","run_id":run.pk}
    errors={}; scanned=0
    try:
        stocks=Stock.objects.filter(watchers__isnull=False,exchange="NSE").distinct().order_by("symbol")[:50]
        instruments={r.get("symbol"):r.get("token") for r in get_instrument_master() if r.get("exch_seg")=="NSE" and r.get("symbol","").endswith("-EQ")}
        broker=get_broker_client()
        for stock in stocks:
            if timezone.now()>=cutoff+timedelta(minutes=10):
                errors["scan"]="scan deadline reached; remaining symbols skipped";break
            scanned+=1
            try:
                token=instruments.get(stock.symbol+"-EQ")
                if not token:
                    errors[stock.symbol]="equity instrument unavailable";continue
                history=list(stock.price_history.filter(date__lt=day).order_by("-date").values("date","open","high","low","close","volume")[:60])[::-1]
                if not history or history[-1]["date"]<day-timedelta(days=7):
                    errors[stock.symbol]="stale daily history";continue
                candles=broker.fetch_candles_for_token("NSE",token,"1m",lookback_days=1,to_date=cutoff)
                bars=sorted([c for c in candles if c["timestamp"].astimezone(IST).date()==day and c["timestamp"]+timedelta(minutes=1)<=cutoff],key=lambda c:c["timestamp"])
                if len(bars)<280 or cutoff-bars[-1]["timestamp"]>timedelta(minutes=2):
                    errors[stock.symbol]="stale intraday data";continue
                setup,why=evaluate_setup(history,bars)
                if setup:
                    BTSTSignal.objects.create(run=run,stock=stock,exit_session=next_session(day),**setup)
                else:
                    errors[stock.symbol]=why
            except Exception as error:
                errors[stock.symbol]=type(error).__name__+": data evaluation failed"
        run.status="completed" if not errors.get("scan") else "partial"
    except Exception as error:
        errors["scan"]=type(error).__name__+": scan failed";run.status="failed"
    run.scanned=scanned;run.details={"reasons":errors,"signal_count":run.signals.count(),"universe":"NSE watchlisted equities, max 50"}
    run.completed_at=timezone.now();run.save()
    return {"status":run.status,"scanned":scanned,"signals":run.signals.count(),"run_id":run.pk}
