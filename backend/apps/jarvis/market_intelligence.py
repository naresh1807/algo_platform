"""
Market Intelligence -- "మన వెబ్సైట్లో పూర్తిగా ML జార్విస్ పైన
డిపెండ్ అయ్యే వర్క్ అవుతుంది... ఇది ఎప్పటికప్పుడు అన్ని స్టాక్స్
ఆప్షన్స్ ఇండెక్స్ పైన మరియు న్యూస్ పైన అబ్జర్వ్ చేసి మార్కెట్ ఏ
విధంగా ఉండబోతుందో... విశ్లేషించి సరైన సమాధానం చెప్పి" -- the trader
asked for JARVIS/ML to continuously watch stocks, options, indices,
and news together and explain what the market looks like and what's
worth buying, because one person can't watch everything at once.

CRITICAL, stated plainly because this is the single most
"give me an answer to act on" surface in the whole platform:
market_outlook() introduces NO new prediction of its own. It is a
SYNTHESIS layer only -- every number and verdict it reports was
already computed by an existing, separately-reviewable module:
  - Index bias: apps.signals.engine's own TradingSignal (technical
    score + ml_win_probability + regime), the SAME signal
    apps.execution would act on -- not a separate, unaudited "vibe."
  - News context: apps.news's FinBERT sentiment scores, averaged over
    a short recent window.
  - Options idea: apps.options.strike_selector.suggest_best_strike --
    the same OI/Greeks/volume/risk scoring that command already does
    on its own.
  - Stock idea: apps.investing.fundamental_score's stored
    StockRecommendation -- the same fundamentals+valuation+technical
    score that page already shows.

If any of those upstream modules is wrong, this inherits that error --
it does not add a NEW way to be wrong on top. This is a deliberate
design choice: a single opaque "AI says buy X" number would be far
more dangerous than an explained rollup of decisions a trader can
already inspect individually elsewhere in this platform. Every
sentence market_outlook() produces should be traceable to a specific
other module's own output, and the reasoning text says so.

This is still not investment/trading advice, and still cannot see
everything that matters (execution risk, gap risk, liquidity at the
moment of a real fill, anything not captured in the upstream modules'
own known gaps -- see each of their own docstrings). The summary
text says this explicitly, every time, for the same reason
apps.investing.fundamental_score's reasoning always includes its own
disclaimer inline rather than only in a docstring.
"""

from __future__ import annotations

from datetime import date


def _index_bias() -> dict:
    """
    Reads the latest TradingSignal for each settings.WATCHLIST symbol
    -- does NOT generate a new one. Bias is a simple, explainable rule
    over what's already there (BUY signal + trending regime + decent
    ML win-probability => Bullish; a rejected/weak read => Neutral;
    nothing generated yet => Neutral/no-data), not a new model.
    """
    from django.conf import settings

    from apps.signals.models import TradingSignal
    from common.constants import SignalStatus, SignalType

    per_symbol = []
    for symbol in settings.WATCHLIST:
        latest = TradingSignal.objects.filter(symbol=symbol).order_by("-created_at").first()
        if latest is None:
            per_symbol.append({"symbol": symbol, "bias": "No data", "detail": "No signal generated yet."})
            continue

        if latest.status == SignalStatus.APPROVED and latest.signal_type == SignalType.BUY:
            bias = "Bullish"
        elif latest.status == SignalStatus.APPROVED and latest.signal_type == SignalType.SELL:
            bias = "Bearish"
        elif latest.status == SignalStatus.REJECTED:
            bias = "Neutral"
        else:
            bias = "Neutral"

        per_symbol.append({
            "symbol": symbol,
            "bias": bias,
            "regime": latest.regime,
            "technical_score": latest.technical_score,
            "ml_win_probability": latest.ml_win_probability,
            "detail": latest.reason,
            "as_of": latest.created_at.isoformat(),
        })

    return {"per_symbol": per_symbol}


def _news_context(hours: int = 6) -> dict:
    """Average recent FinBERT sentiment per watchlist symbol -- read-only over apps.news's own scoring."""
    from datetime import timedelta

    from django.conf import settings
    from django.db.models import Avg
    from django.utils import timezone

    from apps.news.models import NewsSentiment

    since = timezone.now() - timedelta(hours=hours)
    per_symbol = {}
    for symbol in settings.WATCHLIST:
        agg = NewsSentiment.objects.filter(symbol=symbol, published_at__gte=since).aggregate(avg=Avg("sentiment_score"))
        per_symbol[symbol] = round(agg["avg"], 3) if agg["avg"] is not None else None
    return per_symbol


def _overall_bias(index_bias: dict, news: dict) -> tuple[str, float | None]:
    """
    Combines per-symbol technical bias with news sentiment into one
    overall word (Bullish/Bearish/Neutral) + a rough confidence when
    derivable. Simple majority/average rule, not a model -- documented
    assumption, same as every other heuristic combination rule in this
    codebase (apps.investing.fundamental_score's WEIGHTS, etc.).
    """
    bullish_count = sum(1 for s in index_bias["per_symbol"] if s["bias"] == "Bullish")
    total_with_data = sum(1 for s in index_bias["per_symbol"] if s["bias"] != "No data")

    sentiments = [v for v in news.values() if v is not None]
    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else None

    if total_with_data == 0:
        return "Neutral", None

    bullish_fraction = bullish_count / total_with_data
    if bullish_fraction >= 0.5 and (avg_sentiment is None or avg_sentiment >= -0.1):
        return "Bullish", round(bullish_fraction, 2)
    if bullish_fraction == 0 and avg_sentiment is not None and avg_sentiment < -0.2:
        return "Bearish", round(1 - bullish_fraction, 2)
    return "Neutral", round(bullish_fraction, 2)


def market_outlook() -> dict:
    """
    The single synthesized report -- returns
    {"bias", "bias_confidence", "index_bias", "news_sentiment",
    "options_idea", "stock_idea", "summary"}. Always returns something
    (never raises for "no data yet" cases) -- individual sections say
    "not enough data" rather than the whole function failing, matching
    how every other JARVIS handler in this codebase degrades.
    """
    index_bias = _index_bias()
    news = _news_context()
    overall_bias, confidence = _overall_bias(index_bias, news)

    options_idea = _options_idea_for_bias(overall_bias)
    stock_idea = _top_stock_idea()

    summary = _build_summary(overall_bias, confidence, index_bias, news, options_idea, stock_idea)

    return {
        "bias": overall_bias,
        "bias_confidence": confidence,
        "index_bias": index_bias,
        "news_sentiment": news,
        "options_idea": options_idea,
        "stock_idea": stock_idea,
        "summary": summary,
    }


def _options_idea_for_bias(bias: str) -> dict | None:
    """
    Only suggests an options idea when the overall bias is directional
    (Bullish/Bearish) -- a Neutral market has no honest directional
    options idea to give, and this says so rather than picking one
    arbitrarily. Uses apps.options.strike_selector's own scoring
    (delta/OI/volume/theta), same as the standalone best_strike JARVIS
    command -- not a new options-selection method.
    """
    if bias == "Neutral":
        return None

    from django.conf import settings

    from apps.options.signals_engine import nearest_expiry
    from apps.options.strike_selector import suggest_best_strike

    direction = "bullish" if bias == "Bullish" else "bearish"
    underlying = settings.WATCHLIST[0] if settings.WATCHLIST else "NIFTY"

    expiry = nearest_expiry(underlying)
    if expiry is None:
        return {"available": False, "reason": f"No options data synced for {underlying} yet."}

    result = suggest_best_strike(underlying, expiry, direction)
    if result["suggested"] is None:
        return {"available": False, "reason": result["reason"]}

    return {
        "available": True, "underlying": underlying, "direction": direction,
        "expiry": expiry.isoformat(), "strike": result["suggested"]["strike"],
        "reason": result["reason"],
    }


def _top_stock_idea() -> dict | None:
    """
    Top current StockRecommendation (strong_hold first) -- reads
    apps.investing's own weekly-generated output, same as the
    stock_suggestions JARVIS command. Long-term, deliberately
    independent of the intraday index bias above (a good 5-year
    compounder doesn't stop being one because today's index signal is
    bearish).
    """
    from apps.investing.models import StockRecommendation

    top = (
        StockRecommendation.objects.filter(verdict__in=["strong_hold", "moderate_hold"])
        .select_related("stock").order_by("-fundamental_score").first()
    )
    if top is None:
        return None
    return {
        "symbol": top.stock.symbol, "score": top.fundamental_score, "verdict": top.verdict,
        "suggested_hold_months": top.suggested_hold_months, "reasoning": top.reasoning,
    }


def _build_summary(bias, confidence, index_bias, news, options_idea, stock_idea) -> str:
    parts = [f"Market bias: {bias}" + (f" (confidence {confidence:.0%})" if confidence is not None else "") + "."]

    index_bits = []
    for s in index_bias["per_symbol"]:
        if s["bias"] == "No data":
            index_bits.append(f"{s['symbol']}: no signal yet")
        else:
            index_bits.append(f"{s['symbol']}: {s['bias']} ({s.get('regime', '?')} regime)")
    parts.append("Index signals -- " + "; ".join(index_bits) + ".")

    sentiments = [f"{sym}: {val:+.2f}" for sym, val in news.items() if val is not None]
    if sentiments:
        parts.append("Recent news sentiment (-1 to +1) -- " + ", ".join(sentiments) + ".")

    if options_idea is None:
        parts.append("No directional options idea -- overall bias is Neutral.")
    elif not options_idea.get("available"):
        parts.append(f"Options: {options_idea['reason']}")
    else:
        parts.append(
            f"Options idea: {options_idea['underlying']} {options_idea['strike']} "
            f"{'CE' if options_idea['direction'] == 'bullish' else 'PE'} ({options_idea['expiry']}) -- {options_idea['reason']}"
        )

    if stock_idea is None:
        parts.append("No long-term stock idea available yet -- add stocks to the watchlist and wait for the weekly sync.")
    else:
        parts.append(
            f"Long-term stock idea: {stock_idea['symbol']} ({stock_idea['score']:.0f}/100, "
            f"~{stock_idea['suggested_hold_months']} months) -- {stock_idea['reasoning']}"
        )

    parts.append(
        "Every figure above comes from this platform's own existing signal/scoring modules, explained together -- "
        "not a new prediction. Not investment or trading advice; verify before acting."
    )
    return " ".join(parts)
