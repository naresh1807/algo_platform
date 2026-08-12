from django.contrib import admin

from .models import (
    FundamentalSnapshot,
    Index,
    IndexConstituent,
    IndexPriceSnapshot,
    IPOListing,
    Stock,
    StockPriceHistory,
    StockRecommendation,
    StockWatchlist,
)


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("symbol", "name", "sector", "exchange")
    list_filter = ("sector", "exchange")
    search_fields = ("symbol", "name")


@admin.register(FundamentalSnapshot)
class FundamentalSnapshotAdmin(admin.ModelAdmin):
    list_display = ("stock", "reporting_type", "period_end", "revenue_growth_yoy", "profit_growth_yoy", "roe")
    list_filter = ("reporting_type",)
    search_fields = ("stock__symbol",)


@admin.register(StockWatchlist)
class StockWatchlistAdmin(admin.ModelAdmin):
    list_display = ("user", "stock", "added_at")
    list_filter = ("user",)


@admin.register(StockRecommendation)
class StockRecommendationAdmin(admin.ModelAdmin):
    list_display = ("stock", "verdict", "fundamental_score", "suggested_hold_months", "generated_at")
    list_filter = ("verdict",)


@admin.register(IPOListing)
class IPOListingAdmin(admin.ModelAdmin):
    list_display = ("company_name", "status", "open_date", "close_date", "price_band_low", "price_band_high")
    list_filter = ("status",)
    search_fields = ("company_name", "symbol")


@admin.register(Index)
class IndexAdmin(admin.ModelAdmin):
    list_display = ("name", "symbol", "exchange")
    list_filter = ("exchange",)


@admin.register(IndexConstituent)
class IndexConstituentAdmin(admin.ModelAdmin):
    list_display = ("index", "stock", "last_price", "change_pct", "weight_pct", "updated_at")
    list_filter = ("index",)
    search_fields = ("stock__symbol",)


@admin.register(IndexPriceSnapshot)
class IndexPriceSnapshotAdmin(admin.ModelAdmin):
    list_display = ("index", "ltp", "change_pct", "timestamp")
    list_filter = ("index",)


@admin.register(StockPriceHistory)
class StockPriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("stock", "date", "open", "high", "low", "close", "volume")
    list_filter = ("stock",)
    search_fields = ("stock__symbol",)
