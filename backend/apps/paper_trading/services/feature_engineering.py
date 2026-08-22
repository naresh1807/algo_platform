"""
Leakage-safe feature vectors for the AI decision policy. Two stages,
matching the natural decision flow: `build_underlying_features` scores
the underlying BEFORE any contract is chosen (drives the bullish/
bearish/hold call); `augment_with_contract_features` adds the specific
CE/PE candidate's own premium/spread/OI/greeks/time-to-expiry once
contract_selection.py has picked one.

Reuses apps.market_data.indicators.compute_indicators for the core
EMA9/21, MACD, RSI, ATR, ADX, Bollinger-width, relative-volume set --
that function already drops the still-forming candle (leakage-safe) --
and extends it with the spec's remaining requested features (EMA50,
VWAP, candle shape, swing structure, gaps, time-of-day, regime) computed
directly from the same already-completed-candle frame, never duplicating
compute_indicators' own math.

FEATURE_SCHEMA_VERSION is bumped whenever the feature SET changes (not
on every code change) -- apps.learning.ModelRegistry.metrics_json
stores the exact ordered column list a trained model expects (same
convention apps.learning.ml_features already documents), so a model
trained against v1 is never silently fed a v2 vector with a different
shape.
"""

from __future__ import annotations

from datetime import time as dt_time

from django.utils import timezone as django_timezone

from . import market_data_reader

FEATURE_SCHEMA_VERSION = "v1"

MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)
SESSION_MINUTES = (15 * 60 + 30) - (9 * 60 + 15)  # 375


def _regime(adx: float | None, bb_width: float | None) -> str:
    """Local, deliberately simple regime classifier -- NOT a reuse of
    apps.signals.engine.classify_regime, on purpose: this app is a fully
    separate vertical (see models.py's module docstring) and must not
    depend on the other signal engine's internals just for a label.
    Thresholds are approximate/documented, not a calibrated model."""
    if adx is not None and adx >= 25:
        return "trending"
    if bb_width is not None and bb_width >= 0.08:
        return "high_volatility"
    return "sideways"


def _time_of_day_features(now) -> dict:
    local = django_timezone.localtime(now)
    minutes_now = local.hour * 60 + local.minute
    minutes_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
    minutes_close = MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute
    return {
        "minutes_since_open": max(0, min(SESSION_MINUTES, minutes_now - minutes_open)),
        "minutes_to_close": max(0, minutes_close - minutes_now),
    }


def _gap_features(df) -> dict:
    """Compares the first completed bar of the CURRENT session date
    against the immediately prior bar's close (the prior session's last
    traded price) -- a proxy for gap-up/gap-down, not an exchange-
    official gap figure."""
    if df.empty:
        return {"gap_pct": 0.0}
    dates = df["timestamp"].dt.date if "timestamp" in df.columns else df.index.date
    today = dates.iloc[-1] if hasattr(dates, "iloc") else dates[-1]
    todays_rows = df[dates == today] if hasattr(dates, "__eq__") else df
    if todays_rows.empty or len(todays_rows) == len(df):
        return {"gap_pct": 0.0}
    first_today_open = float(todays_rows.iloc[0]["open"])
    prior_close = float(df.iloc[len(df) - len(todays_rows) - 1]["close"])
    if prior_close <= 0:
        return {"gap_pct": 0.0}
    return {"gap_pct": (first_today_open - prior_close) / prior_close * 100}


def build_underlying_features(underlying: str, timeframe: str) -> dict | None:
    from apps.market_data.indicators import compute_indicators

    base = compute_indicators(underlying, timeframe)
    if base is None:
        return None

    df = market_data_reader.latest_completed_candle_frame(underlying, timeframe, limit=300)
    if df.empty or len(df) < 2:
        return None

    close = df["close"].astype(float)
    ema50 = close.ewm(span=50, adjust=False).mean()
    last, prev = df.iloc[-1], df.iloc[-2]
    rng = float(last["high"] - last["low"])
    body = abs(float(last["close"]) - float(last["open"]))
    upper_wick = float(last["high"]) - max(float(last["open"]), float(last["close"]))
    lower_wick = min(float(last["open"]), float(last["close"])) - float(last["low"])

    typical = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3
    vol = df["volume"].astype(float)
    cum_vol = vol.cumsum()
    vwap_series = (typical * vol).cumsum() / cum_vol.replace(0, float("nan"))
    vwap = float(vwap_series.iloc[-1]) if cum_vol.iloc[-1] > 0 else None

    features = dict(base)
    features.update({
        "ema50": float(ema50.iloc[-1]),
        "ema50_slope": float(ema50.iloc[-1] - ema50.iloc[-2]),
        "vwap": vwap,
        "close_vs_vwap_pct": ((float(last["close"]) - vwap) / vwap * 100) if vwap else None,
        "candle_body_ratio": (body / rng) if rng > 0 else 0.0,
        "candle_upper_wick_ratio": (upper_wick / rng) if rng > 0 else 0.0,
        "candle_lower_wick_ratio": (lower_wick / rng) if rng > 0 else 0.0,
        "candle_return_pct": ((float(last["close"]) - float(prev["close"])) / float(prev["close"]) * 100) if prev["close"] else 0.0,
        "swing_high_20": float(df["high"].tail(20).max()),
        "swing_low_20": float(df["low"].tail(20).min()),
        "volume": float(last["volume"]),
        "volume_change_pct": ((float(last["volume"]) - float(prev["volume"])) / float(prev["volume"]) * 100) if prev["volume"] else 0.0,
        "regime": _regime(base.get("adx"), base.get("bb_width")),
    })
    features.update(_gap_features(df))
    features.update(_time_of_day_features(django_timezone.now()))
    return features


def augment_with_contract_features(
    features: dict, *, contract, quote, spot: float, expiry_days: int,
) -> dict:
    """Adds the specific CE/PE candidate's own premium/spread/OI/greeks/
    time-to-expiry once contract_selection.py has resolved one. `quote`
    is a market_data_reader.ContractQuote."""
    out = dict(features)
    strike = float(contract.strike)
    out["strike_distance_from_atm"] = strike - spot
    out["strike_distance_from_atm_pct"] = ((strike - spot) / spot * 100) if spot else None
    out["time_to_expiry_days"] = expiry_days
    out["option_premium"] = quote.ltp
    out["option_bid"] = quote.bid
    out["option_ask"] = quote.ask
    out["option_bid_ask_spread_pct"] = (
        ((quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 100)
        if quote.bid and quote.ask and (quote.ask + quote.bid) > 0 else None
    )
    out["option_volume"] = quote.volume
    out["option_open_interest"] = quote.open_interest
    out["quote_age_seconds"] = quote.age_seconds
    return out


def augment_with_account_context(features: dict, *, account, session_state, consecutive_loss_count: int) -> dict:
    """Current drawdown / previous trade result / consecutive losses /
    cooldown status -- the spec's remaining feature-list items that
    describe the ACCOUNT's own state rather than the market."""
    out = dict(features)
    drawdown_pct = (
        float((account.peak_equity - account.current_equity) / account.peak_equity * 100)
        if account.peak_equity else 0.0
    )
    out["current_drawdown_pct"] = drawdown_pct
    out["consecutive_losses"] = consecutive_loss_count
    out["cooldown_active"] = session_state.state == session_state.State.COOLDOWN
    last_closed_position = account.positions.filter(status="closed").order_by("-closed_at").first()
    out["previous_trade_won"] = (
        bool(last_closed_position.trade.net_pnl > 0)
        if last_closed_position is not None and hasattr(last_closed_position, "trade") else None
    )
    return out
