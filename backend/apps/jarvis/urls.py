from django.urls import path

from .views import (
    JarvisCommandView,
    JarvisHistoryListView,
    JarvisMemoryView,
    JarvisSuggestedCommandsView,
    MarketOutlookHistoryView,
    MarketOutlookView,
)

app_name = "jarvis"

urlpatterns = [
    path("command/", JarvisCommandView.as_view(), name="jarvis-command"),
    path("history/", JarvisHistoryListView.as_view(), name="jarvis-history"),
    path("memory/", JarvisMemoryView.as_view(), name="jarvis-memory"),
    path("suggested/", JarvisSuggestedCommandsView.as_view(), name="jarvis-suggested"),
    path("market-outlook/", MarketOutlookView.as_view(), name="jarvis-market-outlook"),
    path("market-outlook/history/", MarketOutlookHistoryView.as_view(), name="jarvis-market-outlook-history"),
]
