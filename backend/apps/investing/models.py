"""
manual has no chapter for long-term equity investing at all (every
existing app in this codebase -- market_data, signals, execution,
options -- is built around intraday NIFTY/BANKNIFTY index trading).
This app exists because the trader asked for it directly, independent
of the manual: automatic stock suggestions with a suggested holding
period, backed by real fundamental data, plus a stock watchlist and
IPO suggestions.

Deliberately its own app, not bolted onto apps.market_data or
apps.signals -- those two are intraday-candle-and-technical-indicator
machinery; this is quarterly-fundamentals-and-holding-period machinery,
on a completely different time scale (quarters/years, not 5-minute
candles) and a completely different question ("is this a business
worth owning for years" vs "is this a good intraday entry right now").
"""

from django.db import models


class Stock(models.Model):
    """
    One row per equity this app tracks fundamentals for. Deliberately
    separate from any watchlist -- a Stock can exist here (fundamentals
    fetched, scored) without any user having added it to their
    StockWatchlist yet, the same way OptionContract rows exist
    independent of whether a signal ever fires on them.
    """

    class Exchange(models.TextChoices):
        NSE = "NSE", "NSE"
        BSE = "BSE", "BSE"

    symbol = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    sector = models.CharField(max_length=128, blank=True)
    exchange = models.CharField(max_length=8, choices=Exchange.choices, default=Exchange.NSE)
    listed_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "stocks"
        ordering = ["symbol"]

    def __str__(self):
        return f"{self.symbol} ({self.name})"


class FundamentalSnapshot(models.Model):
    """
    One row per stock per reported period (quarterly or annual results)
    -- an append-only history, never overwritten, so
    apps.investing.fundamental_score can compare a company's LATEST
    results against its own trailing quarters to judge whether growth
    is accelerating, steady, or decelerating (the actual thing "results
    are good" means to a trader -- a single quarter's absolute numbers
    mean little without that trend).

    All figures are None, not zero, when the source didn't report them
    -- apps.investing.fundamental_score treats a None input as "can't
    score this factor," never as "this factor is zero," matching every
    other None-means-unknown convention in this codebase (e.g.
    apps.options.broker_client's iv=None).
    """

    class ReportingType(models.TextChoices):
        QUARTERLY = "quarterly", "Quarterly"
        ANNUAL = "annual", "Annual"

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="fundamentals")
    period_end = models.DateField(help_text="Last date of the reported quarter/year.")
    reporting_type = models.CharField(max_length=16, choices=ReportingType.choices)

    revenue = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text="In INR crores.")
    net_profit = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text="In INR crores.")
    eps = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    revenue_growth_yoy = models.FloatField(null=True, blank=True, help_text="% vs same period last year.")
    profit_growth_yoy = models.FloatField(null=True, blank=True, help_text="% vs same period last year.")
    roe = models.FloatField(null=True, blank=True, help_text="Return on equity, %.")
    roce = models.FloatField(null=True, blank=True, help_text="Return on capital employed, %.")
    debt_to_equity = models.FloatField(null=True, blank=True)
    promoter_holding_pct = models.FloatField(null=True, blank=True)
    pe_ratio = models.FloatField(null=True, blank=True)

    source = models.CharField(max_length=64, help_text="e.g. nse_corporate_results")
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fundamental_snapshots"
        ordering = ["-period_end"]
        unique_together = ("stock", "period_end", "reporting_type")
        indexes = [models.Index(fields=["stock", "-period_end"])]

    def __str__(self):
        return f"{self.stock.symbol} {self.reporting_type} @ {self.period_end}"


class StockWatchlist(models.Model):
    """
    A trader's own long-term watchlist -- deliberately a DB-backed,
    per-user model (unlike settings.WATCHLIST's intraday index list,
    which that setting's own comment already flags as "promote to a
    model if this ever needs to be edited from the dashboard instead
    of a redeploy" -- exactly the case here).
    """

    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="stock_watchlist")
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="watchers")
    added_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "stock_watchlist"
        ordering = ["-added_at"]
        unique_together = ("user", "stock")

    def __str__(self):
        return f"{self.user}: {self.stock.symbol}"


class StockRecommendation(models.Model):
    """
    The output of apps.investing.fundamental_score.score_stock() --
    one row per scoring run, kept (not overwritten) so a trader can see
    how a stock's score/verdict has moved over time, the same
    "append-only history" reasoning as FundamentalSnapshot above and
    apps.learning.TradeReview elsewhere in this codebase.

    IMPORTANT: this is a heuristic screening output, not investment
    advice -- see fundamental_score.py's own docstring on what the
    score actually measures and doesn't. `reasoning` always states
    this plainly so it survives being read on its own (dashboard card,
    JARVIS response) without the caveat living only in code comments
    no trader will ever see.
    """

    class Verdict(models.TextChoices):
        STRONG_HOLD = "strong_hold", "Strong long-term hold candidate"
        MODERATE_HOLD = "moderate_hold", "Moderate hold candidate"
        WATCH = "watch", "Watch, not yet a hold candidate"
        AVOID = "avoid", "Fundamentals don't support a hold right now"

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="recommendations")
    based_on = models.ForeignKey(
        FundamentalSnapshot, on_delete=models.SET_NULL, null=True, blank=True,
        help_text="The latest snapshot this score was computed from.",
    )
    fundamental_score = models.FloatField(
        help_text="0-100 composite heuristic score -- fundamentals, valuation, AND technical trend combined (see apps.investing.fundamental_score.WEIGHTS). Named for backward compatibility with the field's original fundamentals-only version.",
    )
    verdict = models.CharField(max_length=16, choices=Verdict.choices)
    suggested_hold_months = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="None when verdict is WATCH/AVOID -- there is no hold suggestion to make.",
    )
    valuation_label = models.CharField(
        max_length=16, blank=True,
        help_text="Low/Medium/High -- see apps.investing.valuation. Blank if PE data was unavailable.",
    )
    technical_trend = models.CharField(
        max_length=24, blank=True,
        help_text="See apps.investing.technical_score. Blank if insufficient price history was available.",
    )
    reasoning = models.TextField()
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stock_recommendations"
        ordering = ["-generated_at"]
        indexes = [models.Index(fields=["stock", "-generated_at"])]

    def __str__(self):
        return f"{self.stock.symbol}: {self.verdict} ({self.fundamental_score:.0f}/100)"


class StockPriceHistory(models.Model):
    """
    Daily OHLC bars per stock -- deliberately separate from
    apps.market_data.HistoricalData, which is intraday (5m-class)
    candles for the small, fixed settings.WATCHLIST index list. This
    is daily-resolution, open-ended over the whole exchange, and
    exists specifically to feed apps.investing.technical_score's
    moving-average/RSI trend read and apps.investing.valuation's
    52-week-range context -- neither of which needs or wants intraday
    granularity for a holding-period-measured-in-months decision.
    """

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="price_history")
    date = models.DateField(db_index=True)
    open = models.DecimalField(max_digits=12, decimal_places=2)
    high = models.DecimalField(max_digits=12, decimal_places=2)
    low = models.DecimalField(max_digits=12, decimal_places=2)
    close = models.DecimalField(max_digits=12, decimal_places=2)
    volume = models.BigIntegerField(null=True, blank=True)
    source = models.CharField(max_length=64, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stock_price_history"
        ordering = ["-date"]
        unique_together = ("stock", "date")
        indexes = [models.Index(fields=["stock", "-date"])]

    def __str__(self):
        return f"{self.stock.symbol} {self.date}: {self.close}"


class Index(models.Model):
    """
    A market index (NIFTY 50, NIFTY BANK, SENSEX, ...) -- the trader
    asked for a broker-app-style dashboard: index ticker cards with
    live price movement, click through to the list of constituent
    companies. `symbol` is the broker-style ticker used for display
    and for apps.investing.fundamentals_client.get_index_snapshot's
    NSE API calls (e.g. "NIFTY 50", "NIFTY BANK") -- NSE's own index
    naming, not always the same short form as settings.WATCHLIST's
    "NIFTY"/"BANKNIFTY" (those are the F&O/index-future symbols
    apps.market_data trades intraday; this is the cash index's own
    display name).
    """

    class Exchange(models.TextChoices):
        NSE = "NSE", "NSE"
        BSE = "BSE", "BSE"

    name = models.CharField(max_length=64, unique=True, help_text='e.g. "NIFTY 50", "NIFTY BANK", "SENSEX"')
    symbol = models.CharField(max_length=32, help_text="Broker-style display ticker, e.g. NIFTY, BANKNIFTY, SENSEX.")
    exchange = models.CharField(max_length=8, choices=Exchange.choices, default=Exchange.NSE)

    class Meta:
        db_table = "indices"
        ordering = ["name"]

    def __str__(self):
        return self.name


class IndexConstituent(models.Model):
    """
    Which stocks belong to which index, right now -- populated by
    apps.investing.tasks.sync_index_constituents_and_prices from NSE's
    real index-snapshot endpoint (one API call returns the WHOLE
    membership list plus every member's current price in a single
    response -- see apps.investing.fundamentals_client.get_index_snapshot).

    `last_price`/`change_pct` are a lightweight live-ish cache for the
    dashboard's constituent-list drill-down -- NOT a substitute for
    StockPriceHistory's own daily bars (that's the historical record
    apps.investing.technical_score reads; this is just "what did the
    last sync say the price was," refreshed every few minutes during
    market hours, matching apps.monitoring.PriceAlert's own refresh
    cadence).
    """

    index = models.ForeignKey(Index, on_delete=models.CASCADE, related_name="constituents")
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="index_memberships")
    weight_pct = models.FloatField(null=True, blank=True, help_text="Index weight, when NSE's response includes it.")
    last_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    change_pct = models.FloatField(null=True, blank=True, help_text="% change from previous close, from the last sync.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "index_constituents"
        ordering = ["-weight_pct"]
        unique_together = ("index", "stock")

    def __str__(self):
        return f"{self.stock.symbol} in {self.index.name}"


class IndexPriceSnapshot(models.Model):
    """
    The index's OWN live-ish price (not a constituent's) -- what
    drives the dashboard's ticker-card LTP/change%, refreshed on the
    same sync as IndexConstituent above. Append-only history (never
    overwritten), same "keep the time series" reasoning as
    apps.risk.EquitySnapshot -- lets a later feature chart the index's
    own intraday movement from this app's own data, without needing to
    reach into apps.market_data.HistoricalData (which only tracks
    NIFTY/BANKNIFTY, not every index this app can show).
    """

    index = models.ForeignKey(Index, on_delete=models.CASCADE, related_name="price_snapshots")
    timestamp = models.DateTimeField(db_index=True)
    ltp = models.DecimalField(max_digits=12, decimal_places=2)
    change = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    change_pct = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "index_price_snapshots"
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["index", "-timestamp"])]

    def __str__(self):
        return f"{self.index.name} {self.ltp} @ {self.timestamp}"


class IPOListing(models.Model):
    """
    Upcoming/recent IPOs -- populated by
    apps.investing.tasks.sync_ipo_calendar from a real (documented,
    but see that module's own honesty caveat) public source, never
    fabricated. `notes` carries the same kind of heuristic reasoning
    StockRecommendation.reasoning does when apps.investing.ipo_score
    has enough data to say anything (subscription numbers, sector) --
    blank when there isn't enough public information yet to say
    anything meaningful, which is common for an IPO that hasn't opened.
    """

    class Status(models.TextChoices):
        UPCOMING = "upcoming", "Upcoming"
        OPEN = "open", "Open for subscription"
        CLOSED = "closed", "Closed, awaiting listing"
        LISTED = "listed", "Listed"

    company_name = models.CharField(max_length=255)
    symbol = models.CharField(max_length=32, blank=True, help_text="Assigned once listed.")
    exchange = models.CharField(max_length=16, blank=True, help_text="e.g. NSE, BSE, NSE+BSE")
    open_date = models.DateField(null=True, blank=True)
    close_date = models.DateField(null=True, blank=True)
    listing_date = models.DateField(null=True, blank=True)
    price_band_low = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_band_high = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    issue_size_crores = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPCOMING)
    subscription_times = models.FloatField(
        null=True, blank=True, help_text="Overall subscription multiple, once available.",
    )
    notes = models.TextField(blank=True)
    source = models.CharField(max_length=64, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ipo_listings"
        ordering = ["-open_date"]

    def __str__(self):
        return f"{self.company_name} [{self.status}]"
