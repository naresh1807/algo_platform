from django.db import models

from common.constants import SentimentLabel


class NewsSentiment(models.Model):
    """
    manual section 7: news_sentiment. One row per headline/article,
    scored by the FinBERT sentiment engine (apps/news/finbert.py).

    sentiment_score and confidence are kept separate on purpose: a
    strongly-worded headline that the model is unsure about (low
    confidence) should be treated differently by the signal engine than
    a mild headline the model is very confident about -- collapsing both
    into a single number would lose that distinction.
    """

    symbol = models.CharField(max_length=32, db_index=True, blank=True)
    headline = models.TextField()
    source = models.CharField(max_length=128)
    published_at = models.DateTimeField(db_index=True)
    sentiment_label = models.CharField(max_length=16, choices=SentimentLabel.choices)
    sentiment_score = models.FloatField(help_text="Normalized -1.0 (very negative) to +1.0 (very positive)")
    confidence = models.FloatField(help_text="Model confidence 0.0-1.0")
    news_category = models.CharField(
        max_length=64, blank=True,
        help_text="e.g. earnings, macro, regulatory, company-specific",
    )

    class Meta:
        db_table = "news_sentiment"
        ordering = ["-published_at"]
        indexes = [models.Index(fields=["symbol", "-published_at"])]

    def __str__(self):
        return f"{self.symbol or 'MARKET'} [{self.sentiment_label}] {self.headline[:60]}"


class NewsImpactAnalysis(models.Model):
    """
    manual section 10, "Output format": sector impact, index impact,
    likely rising/falling stocks, confidence, time horizon, trade
    relevance. One row per NewsSentiment row -- kept as a SEPARATE
    model (OneToOne) rather than extra columns on NewsSentiment itself,
    since this is a distinct enrichment STEP in the pipeline (entity
    extraction + impact scoring, apps.news.impact_engine) that runs
    after sentiment scoring and could reasonably fail/be skipped
    independently of it (e.g. a headline FinBERT scores fine but that
    matches no known entity/sector at all -- see impact_engine's
    docstring for how that case is handled).
    """

    class TradeRelevance(models.TextChoices):
        DIRECT = "direct", "Direct (named stock mentioned)"
        SECTOR = "sector", "Sector-wide"
        MACRO = "macro", "Macro / index-wide"
        NONE = "none", "No clear market relevance"

    news = models.OneToOneField(NewsSentiment, on_delete=models.CASCADE, related_name="impact_analysis")
    sectors = models.JSONField(default=list, help_text="Sector names this headline was matched to.")
    likely_rising_stocks = models.JSONField(default=list, help_text="Tickers, if sentiment was positive and directly mentioned.")
    likely_falling_stocks = models.JSONField(default=list, help_text="Tickers, if sentiment was negative and directly mentioned.")
    index_impact = models.CharField(
        max_length=16, blank=True,
        help_text="Rough directional read on NIFTY/BANKNIFTY if this headline is macro/sector-wide, else blank.",
    )
    confidence = models.FloatField(help_text="Same FinBERT confidence carried over -- see impact_engine for why.")
    time_horizon = models.CharField(max_length=16, help_text="intraday / multi_day / unspecified")
    trade_relevance = models.CharField(max_length=16, choices=TradeRelevance.choices)

    class Meta:
        db_table = "news_impact_analysis"

    def __str__(self):
        return f"Impact({self.news.symbol or 'MARKET'}): {self.trade_relevance}"
