from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from .fundamental_score import score_stock
from .models import (
    FundamentalSnapshot,
    Index,
    IndexConstituent,
    IPOListing,
    Stock,
    StockRecommendation,
    StockWatchlist,
)


class StockModelTests(TestCase):
    def test_str(self):
        stock = Stock.objects.create(symbol="TCS", name="Tata Consultancy Services")
        self.assertIn("TCS", str(stock))


class FundamentalScoreTests(TestCase):
    """apps.investing.fundamental_score -- the actual scoring/hold-period logic."""

    def setUp(self):
        self.stock = Stock.objects.create(symbol="STRONGCO", name="Strong Company Ltd")

    def _snapshot(self, quarters_ago, **kwargs):
        # Steps back `quarters_ago` quarters from June 2026, handling
        # year rollover correctly via floor-division month arithmetic
        # (plain "month - quarters_ago*3" breaks once it goes <= 0).
        base_year, base_month = 2026, 6
        total_months = base_month - quarters_ago * 3
        year = base_year + (total_months - 1) // 12
        month = (total_months - 1) % 12 + 1
        defaults = dict(stock=self.stock, reporting_type="quarterly", period_end=date(year, month, 28))
        defaults.update(kwargs)
        return FundamentalSnapshot.objects.create(**defaults)

    def test_no_snapshots_returns_none(self):
        self.assertIsNone(score_stock(self.stock))

    def test_insufficient_factors_returns_none(self):
        # Only 2 of 7 factors present -- below MIN_FACTORS_REQUIRED (3)
        self._snapshot(0, revenue_growth_yoy=15.0, profit_growth_yoy=20.0)
        self.assertIsNone(score_stock(self.stock))

    def test_strong_fundamentals_score_high_and_suggest_long_hold(self):
        self._snapshot(
            0, revenue_growth_yoy=25.0, profit_growth_yoy=30.0, roe=22.0, roce=20.0,
            debt_to_equity=0.2, promoter_holding_pct=65.0,
        )
        result = score_stock(self.stock)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["score"], 80)
        self.assertEqual(result["verdict"], "strong_hold")
        self.assertGreaterEqual(result["suggested_hold_months"], 36)
        self.assertIn("not investment advice", result["reasoning"])

    def test_weak_fundamentals_score_low_and_no_hold_suggestion(self):
        self._snapshot(
            0, revenue_growth_yoy=-5.0, profit_growth_yoy=-15.0, roe=3.0, roce=4.0,
            debt_to_equity=2.5, promoter_holding_pct=10.0,
        )
        result = score_stock(self.stock)
        self.assertIsNotNone(result)
        self.assertLess(result["score"], 35)
        self.assertEqual(result["verdict"], "avoid")
        self.assertIsNone(result["suggested_hold_months"])

    def test_growth_consistency_rewards_multiple_good_quarters(self):
        for i in range(4):
            self._snapshot(
                i, revenue_growth_yoy=15.0, profit_growth_yoy=18.0, roe=18.0, roce=15.0,
                debt_to_equity=0.5, promoter_holding_pct=50.0,
            )
        result = score_stock(self.stock)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["score"], 65)


class StockWatchlistAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="a-real-password-123")
        self.user.groups.add(Group.objects.get_or_create(name="Trader")[0])
        self.client.force_login(self.user)

    def test_create_watchlist_entry_by_symbol(self):
        response = self.client.post("/api/investing/watchlist/", {"stock_symbol": "infy"})
        self.assertEqual(response.status_code, 201)
        entry = StockWatchlist.objects.get(user=self.user)
        self.assertEqual(entry.stock.symbol, "INFY")  # normalized to uppercase

    def test_watchlist_scoped_to_requesting_user(self):
        other = get_user_model().objects.create_user(username="trader2", password="a-real-password-456")
        stock = Stock.objects.create(symbol="WIPRO", name="Wipro Ltd")
        StockWatchlist.objects.create(user=other, stock=stock)

        response = self.client.get("/api/investing/watchlist/")
        self.assertEqual(response.data["count"], 0)  # trader1 sees none of trader2's watchlist


class IPOListingTests(TestCase):
    def test_str_includes_status(self):
        ipo = IPOListing.objects.create(company_name="Example Corp", status="upcoming")
        self.assertIn("upcoming", str(ipo))


class ValuationTests(TestCase):
    """apps.investing.valuation -- Low/Medium/High price assessment."""

    def setUp(self):
        self.stock = Stock.objects.create(symbol="VALCO", name="Valuation Test Co")

    def test_no_pe_data_returns_none(self):
        from .valuation import assess_valuation
        self.assertIsNone(assess_valuation(self.stock))

    def test_absolute_fallback_with_insufficient_history(self):
        from .valuation import assess_valuation
        FundamentalSnapshot.objects.create(
            stock=self.stock, reporting_type="quarterly", period_end=date(2026, 6, 30), pe_ratio=12.0,
        )
        result = assess_valuation(self.stock)
        self.assertEqual(result["method"], "absolute")
        self.assertEqual(result["label"], "Low")  # PE 12 < 25

    def test_percentile_method_with_enough_history(self):
        from .valuation import assess_valuation
        # Current PE (10, at period_end index 0 i.e. most recent) is
        # the cheapest of 4 -- should rank "Low". i only goes 0-3 here,
        # so 6-i stays a valid month (3-6) without needing year rollover.
        for i, pe in enumerate([10.0, 20.0, 25.0, 30.0]):
            FundamentalSnapshot.objects.create(
                stock=self.stock, reporting_type="quarterly",
                period_end=date(2026, 6 - i, 28), pe_ratio=pe,
            )
        result = assess_valuation(self.stock)
        self.assertEqual(result["method"], "percentile")
        self.assertEqual(result["current_pe"], 10.0)
        self.assertEqual(result["label"], "Low")


class TechnicalScoreTests(TestCase):
    """apps.investing.technical_score -- moving average + RSI trend read."""

    def setUp(self):
        self.stock = Stock.objects.create(symbol="TECHCO", name="Technical Test Co")

    def _add_bars(self, closes, start_date=date(2025, 1, 1)):
        from datetime import timedelta
        from .models import StockPriceHistory
        for i, close in enumerate(closes):
            StockPriceHistory.objects.create(
                stock=self.stock, date=start_date + timedelta(days=i),
                open=close, high=close, low=close, close=close,
            )

    def test_insufficient_history_returns_none(self):
        from .technical_score import compute_technical_score
        self._add_bars([100] * 10)  # well under MIN_DAYS_FOR_SMA50
        self.assertIsNone(compute_technical_score(self.stock))

    def test_clear_uptrend_scores_high(self):
        from .technical_score import compute_technical_score
        # Steadily rising closes over 60 days -- price ends well above its own SMA50.
        closes = [100 + i * 1.5 for i in range(60)]
        self._add_bars(closes)
        result = compute_technical_score(self.stock)
        self.assertIsNotNone(result)
        self.assertIn(result["trend"], ("Uptrend", "Strong Uptrend"))
        self.assertGreater(result["score"], 60)

    def test_clear_downtrend_scores_low(self):
        from .technical_score import compute_technical_score
        closes = [200 - i * 1.5 for i in range(60)]
        self._add_bars(closes)
        result = compute_technical_score(self.stock)
        self.assertIsNotNone(result)
        self.assertIn(result["trend"], ("Downtrend", "Strong Downtrend"))
        self.assertLess(result["score"], 40)


class CombinedScoringTests(TestCase):
    """fundamental_score.score_stock now folds in valuation + technical too."""

    def setUp(self):
        self.stock = Stock.objects.create(symbol="ALLGOOD", name="All Good Co")

    def test_recommendation_carries_valuation_and_technical_labels(self):
        from datetime import timedelta
        from .models import StockPriceHistory

        FundamentalSnapshot.objects.create(
            stock=self.stock, reporting_type="quarterly", period_end=date(2026, 6, 30),
            revenue_growth_yoy=20.0, profit_growth_yoy=25.0, roe=20.0, roce=18.0,
            debt_to_equity=0.3, promoter_holding_pct=60.0, pe_ratio=15.0,
        )
        closes = [100 + i * 1.5 for i in range(60)]
        for i, close in enumerate(closes):
            StockPriceHistory.objects.create(
                stock=self.stock, date=date(2025, 1, 1) + timedelta(days=i),
                open=close, high=close, low=close, close=close,
            )

        result = score_stock(self.stock)
        self.assertIsNotNone(result)
        self.assertIn(result["valuation_label"], ("Low", "Medium", "High"))
        self.assertIn(result["technical_trend"], ("Uptrend", "Strong Uptrend", "Sideways / Mixed"))
        self.assertIn("Technical:", result["reasoning"])
        self.assertIn("Price:", result["reasoning"])


class ModelIntegrityTests(TestCase):
    """
    Regression test for the real bug found in this pass: IPOListing's
    class declaration line was accidentally dropped in an earlier edit,
    leaving its fields nested inside StockPriceHistory instead of being
    their own top-level model. py_compile didn't catch it (nesting a
    class is syntactically valid Python) -- only actually using the
    model would have. This test exists so that class of mistake fails
    loudly here instead of silently shipping again.
    """

    def test_every_investing_model_is_independently_usable(self):
        import apps.investing.models as models_module

        for name in ("Stock", "FundamentalSnapshot", "StockWatchlist", "StockRecommendation",
                     "StockPriceHistory", "Index", "IndexConstituent", "IndexPriceSnapshot", "IPOListing"):
            model = getattr(models_module, name)
            # A model whose fields got nested inside a sibling class
            # either isn't a real Model subclass at that module level,
            # or fails to build a queryset -- either failure surfaces here.
            self.assertTrue(hasattr(model, "objects"), f"{name} has no manager -- not a real top-level model")
            list(model.objects.all()[:1])  # exercises the actual DB-facing manager, not just attribute presence


class IndexModelTests(TestCase):
    def test_index_str(self):
        index = Index.objects.create(name="NIFTY 50", symbol="NIFTY")
        self.assertEqual(str(index), "NIFTY 50")

    def test_constituent_str_includes_index_name(self):
        index = Index.objects.create(name="NIFTY BANK", symbol="BANKNIFTY")
        stock = Stock.objects.create(symbol="HDFCBANK", name="HDFC Bank Ltd")
        constituent = IndexConstituent.objects.create(index=index, stock=stock, last_price=1650.50)
        self.assertIn("HDFCBANK", str(constituent))
        self.assertIn("NIFTY BANK", str(constituent))


class SeedIndicesCommandTests(TestCase):
    def test_seed_creates_default_indices(self):
        from django.core.management import call_command

        call_command("seed_indices")
        self.assertTrue(Index.objects.filter(name="NIFTY 50").exists())
        self.assertTrue(Index.objects.filter(name="SENSEX", exchange="BSE").exists())

    def test_seed_is_idempotent(self):
        from django.core.management import call_command

        call_command("seed_indices")
        first_count = Index.objects.count()
        call_command("seed_indices")
        self.assertEqual(Index.objects.count(), first_count)


class TrackedStocksQuerysetTests(TestCase):
    """apps.investing.tasks._tracked_stocks_queryset -- watchlisted OR an index constituent."""

    def test_includes_index_constituents_not_just_watchlist(self):
        from .tasks import _tracked_stocks_queryset

        watchlisted_stock = Stock.objects.create(symbol="WATCHED", name="Watched Co")
        index_only_stock = Stock.objects.create(symbol="INDEXED", name="Indexed Co")
        neither_stock = Stock.objects.create(symbol="NEITHER", name="Untracked Co")

        user = get_user_model().objects.create_user(username="trader1", password="a-real-password-123")
        StockWatchlist.objects.create(user=user, stock=watchlisted_stock)

        index = Index.objects.create(name="NIFTY 50", symbol="NIFTY")
        IndexConstituent.objects.create(index=index, stock=index_only_stock)

        tracked_symbols = set(_tracked_stocks_queryset().values_list("symbol", flat=True))
        self.assertIn("WATCHED", tracked_symbols)
        self.assertIn("INDEXED", tracked_symbols)
        self.assertNotIn("NEITHER", tracked_symbols)


class SyncIndexPricesTaskTests(TestCase):
    def test_closed_market_skips_before_any_sync_or_external_client(self):
        from .tasks import sync_index_constituents_and_prices

        Index.objects.create(name="SENSEX", symbol="SENSEX", exchange="BSE")

        with (
            patch("apps.investing.tasks.is_market_open", return_value=(False, "market closed")),
            patch("apps.investing.tasks._sync_index_prices_via_angelone") as sync_prices,
            patch("apps.investing.tasks._sync_nse_indices") as sync_nse,
            patch("apps.investing.tasks._sync_bse_indices") as sync_bse,
        ):
            result = sync_index_constituents_and_prices()

        self.assertEqual(result, {"skipped": True, "reason": "market closed"})
        sync_prices.assert_not_called()
        sync_nse.assert_not_called()
        sync_bse.assert_not_called()

    def test_bse_sensex_sync_is_hermetic(self):
        """SENSEX dispatch is covered without making a real BSE request."""
        from .tasks import sync_index_constituents_and_prices

        Index.objects.create(name="SENSEX", symbol="SENSEX", exchange="BSE")
        with (
            patch("apps.investing.tasks.is_market_open", return_value=(True, "")),
            patch("apps.investing.tasks._sync_index_prices_via_angelone", return_value={}),
            patch(
                "apps.investing.bse_client.BSEDataClient.get_sensex_snapshot",
                return_value=None,
            ) as get_snapshot,
            patch(
                "apps.investing.bse_client.BSEDataClient.get_sensex_constituents",
                return_value=[],
            ) as get_constituents,
        ):
            result = sync_index_constituents_and_prices()

        self.assertIn("SENSEX", result)
        self.assertEqual(result["SENSEX"].get("members_synced"), 0)
        get_snapshot.assert_called_once_with()
        get_constituents.assert_called_once_with()

    def test_unsupported_bse_index_is_explicitly_skipped(self):
        """A BSE index other than SENSEX has no client support yet -- should say so, not silently no-op or crash."""
        from .tasks import sync_index_constituents_and_prices

        Index.objects.create(name="BSE 500", symbol="BSE500", exchange="BSE")
        with (
            patch("apps.investing.tasks.is_market_open", return_value=(True, "")),
            patch("apps.investing.tasks._sync_index_prices_via_angelone", return_value={}),
        ):
            result = sync_index_constituents_and_prices()
        self.assertTrue(result["BSE 500"].get("skipped"))
