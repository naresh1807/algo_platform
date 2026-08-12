"""
Shared choices/enums used across apps, kept in one place so risk, signals,
and execution all agree on the same vocabulary (e.g. a "signal_type" of
"BUY" must mean the same thing everywhere).
"""

from django.db import models


class SignalType(models.TextChoices):
    BUY = "buy", "Buy"
    SELL = "sell", "Sell"
    NO_TRADE = "no_trade", "No Trade"


class SignalStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    EXECUTED = "executed", "Executed"
    EXPIRED = "expired", "Expired"


class PositionSide(models.TextChoices):
    LONG = "long", "Long"
    SHORT = "short", "Short"


class MarketRegime(models.TextChoices):
    TRENDING = "trending", "Trending"
    SIDEWAYS = "sideways", "Sideways"
    HIGH_VOLATILITY = "high_volatility", "High Volatility / Event-Driven"


class RiskEventSeverity(models.TextChoices):
    INFO = "info", "Info"
    WARNING = "warning", "Warning"
    CRITICAL = "critical", "Critical"  # critical events are what the kill switch watches


class SentimentLabel(models.TextChoices):
    POSITIVE = "positive", "Positive"
    NEUTRAL = "neutral", "Neutral"
    NEGATIVE = "negative", "Negative"
