from datetime import timedelta
from unittest.mock import patch
from django.test import SimpleTestCase
from django.utils import timezone
from .rss_client import fetch_war_headlines


class WarFeedTests(SimpleTestCase):
    def item(self, title="Ceasefire talks resume", hours=1, url="https://example.com/news"):
        return {"headline":title,"source":"Test source","published_at":(timezone.now()-timedelta(hours=hours)).isoformat(),"url":url}

    @patch("apps.news.rss_client._fetch_google_news_search")
    def test_deduplicates_and_filters_date_topic_and_links(self, fetch):
        good=self.item()
        fetch.return_value=[good,good,self.item(hours=25),self.item(hours=-1),self.item("Software earnings beat estimates"),self.item(url="javascript:alert(1)")]
        result=fetch_war_headlines()
        self.assertEqual(result,[good])

    @patch("apps.news.rss_client._fetch_google_news_search")
    def test_hormuz_and_houthi_coverage(self, fetch):
        fetch.return_value=[self.item("Hormuz shipping disrupted"),self.item("Houthi attacks hit shipping")]
        self.assertEqual(len(fetch_war_headlines()),2)

    @patch("apps.news.rss_client._fetch_google_news_search")
    def test_empty_source_returns_empty_not_fake_headlines(self, fetch):
        fetch.return_value=[]
        self.assertEqual(fetch_war_headlines(),[])
