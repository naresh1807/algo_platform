from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from .entities import classify_time_horizon, extract_entities
from .impact_engine import analyze_impact
from .models import NewsImpactAnalysis, NewsSentiment
from .scoring import aggregate_sentiment


class EntityExtractionTests(TestCase):
    def test_extracts_named_stock(self):
        result = extract_entities("HDFC Bank shares rally on strong quarterly results")
        self.assertIn("HDFCBANK", result["stocks"])

    def test_word_boundary_avoids_false_positive(self):
        """
        This is exactly why _word_boundary_match uses \\b rather than
        plain substring matching -- "itc" as a bare substring would
        wrongly match inside an unrelated word like "capacity".
        """
        result = extract_entities("Factory capacity expansion announced")
        self.assertNotIn("ITC", result["stocks"])

    def test_extracts_sector(self):
        result = extract_entities("RBI policy meeting raises repo rate concerns")
        self.assertIn("banking", result["sectors"])
        self.assertIn("macro", result["sectors"])

    def test_time_horizon_classification(self):
        self.assertEqual(classify_time_horizon("Sensex opens higher this morning"), "intraday")
        self.assertEqual(classify_time_horizon("Company reports Q2 quarterly results"), "multi_day")
        self.assertEqual(classify_time_horizon("Generic market news"), "unspecified")


class SentimentAggregationTests(TestCase):
    def test_no_headlines_returns_neutral_default(self):
        result = aggregate_sentiment("NIFTY")
        self.assertEqual(result["sentiment_score"], 0.0)
        self.assertEqual(result["headline_count"], 0)

    def test_low_confidence_headlines_excluded(self):
        NewsSentiment.objects.create(
            symbol="NIFTY", headline="Test", source="test",
            published_at=timezone.now(), sentiment_label="positive",
            sentiment_score=0.9, confidence=0.3,  # below MIN_CONFIDENCE_TO_COUNT
        )
        result = aggregate_sentiment("NIFTY")
        self.assertEqual(result["headline_count"], 0)

    def test_index_irrelevant_headline_does_not_veto_nifty(self):
        """
        A single named-stock earnings miss (apps.news.rss_client's
        GENERAL_MARKET_FEEDS pulls in this kind of headline under
        symbol="NIFTY" too, not just genuinely index-wide news) must
        NOT count as a "strong contradictory headline" for NIFTY --
        this is the exact real-world case that was vetoing ~93% of
        every NIFTY/BANKNIFTY evaluation before this filter existed.
        """
        news = NewsSentiment.objects.create(
            symbol="NIFTY", headline="PI Industries plunges 10% on weak Q1 results",
            source="test", published_at=timezone.now(),
            sentiment_label="negative", sentiment_score=-0.97, confidence=0.97,
        )
        analyze_impact(news)  # entities["stocks"] -> trade_relevance=DIRECT, index_impact=""

        result = aggregate_sentiment("NIFTY")
        self.assertEqual(result["headline_count"], 0)
        self.assertFalse(result["has_contradictory_headline"])

    def test_genuine_macro_headline_still_vetoes_nifty(self):
        news = NewsSentiment.objects.create(
            symbol="NIFTY", headline="US Fed hikes rates sharply, global markets tumble",
            source="test", published_at=timezone.now(),
            sentiment_label="negative", sentiment_score=-0.9, confidence=0.9,
        )
        analyze_impact(news)  # "macro" sector ("fed hike"/"global markets") -> index_impact="NIFTY_DOWN"

        result = aggregate_sentiment("NIFTY")
        self.assertEqual(result["headline_count"], 1)
        self.assertTrue(result["has_contradictory_headline"])

    def test_headline_with_no_impact_analysis_does_not_veto(self):
        # No analyze_impact() call -- simulates a legacy row or a failed
        # enrichment step; must not count as relevant for NIFTY/BANKNIFTY.
        NewsSentiment.objects.create(
            symbol="NIFTY", headline="Some unenriched headline", source="test",
            published_at=timezone.now(), sentiment_label="negative",
            sentiment_score=-0.9, confidence=0.9,
        )
        result = aggregate_sentiment("NIFTY")
        self.assertEqual(result["headline_count"], 0)

    def test_non_index_symbol_keeps_unfiltered_behavior(self):
        # No index_impact concept applies outside NIFTY/BANKNIFTY --
        # relevance filtering is a no-op for any other symbol.
        NewsSentiment.objects.create(
            symbol="RELIANCE", headline="Some headline", source="test",
            published_at=timezone.now(), sentiment_label="negative",
            sentiment_score=-0.9, confidence=0.9,
        )
        result = aggregate_sentiment("RELIANCE")
        self.assertEqual(result["headline_count"], 1)


class ImpactEngineTests(TestCase):
    def test_analyze_impact_is_idempotent(self):
        """
        update_or_create means calling this twice for the same
        NewsSentiment row replaces the analysis rather than erroring on
        the OneToOne constraint -- important since apps.news.tasks
        could plausibly re-run analysis on a headline.
        """
        news = NewsSentiment.objects.create(
            symbol="NIFTY", headline="RBI cuts repo rate, banking stocks surge",
            source="test", published_at=timezone.now(),
            sentiment_label="positive", sentiment_score=0.8, confidence=0.9,
        )
        analyze_impact(news)
        analyze_impact(news)  # should not raise
        self.assertEqual(NewsImpactAnalysis.objects.filter(news=news).count(), 1)

    def test_direct_stock_mention_buckets_correctly(self):
        news = NewsSentiment.objects.create(
            symbol="", headline="Infosys shares fall on weak guidance",
            source="test", published_at=timezone.now(),
            sentiment_label="negative", sentiment_score=-0.7, confidence=0.9,
        )
        analysis = analyze_impact(news)
        self.assertIn("INFY", analysis.likely_falling_stocks)
        self.assertEqual(analysis.trade_relevance, NewsImpactAnalysis.TradeRelevance.DIRECT)
