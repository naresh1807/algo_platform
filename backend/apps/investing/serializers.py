from rest_framework import serializers

from .models import (
    FundamentalSnapshot,
    Index,
    IndexConstituent,
    IPOListing,
    Stock,
    StockRecommendation,
    StockWatchlist,
)


class StockSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stock
        fields = "__all__"


class FundamentalSnapshotSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="stock.symbol", read_only=True)

    class Meta:
        model = FundamentalSnapshot
        fields = "__all__"


class StockWatchlistSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="stock.symbol", read_only=True)
    name = serializers.CharField(source="stock.name", read_only=True)
    # Accept a plain symbol string on write (POST {"symbol": "TCS"}) --
    # the trader thinks in symbols, not internal Stock IDs; a nested
    # writable-by-symbol field is friendlier than requiring the client
    # to look up the Stock's numeric id first, same reasoning
    # apps.jarvis.responses uses symbol strings everywhere instead of FKs.
    stock_symbol = serializers.CharField(write_only=True, max_length=32)

    class Meta:
        model = StockWatchlist
        fields = ("id", "stock", "symbol", "name", "added_at", "notes", "stock_symbol")
        read_only_fields = ("stock",)

    def create(self, validated_data):
        symbol = validated_data.pop("stock_symbol").strip().upper()
        stock, _ = Stock.objects.get_or_create(symbol=symbol, defaults={"name": symbol})
        user = validated_data.get("user")
        if StockWatchlist.objects.filter(user=user, stock=stock).exists():
            raise serializers.ValidationError({"stock_symbol": "This stock is already on your watchlist."})
        validated_data["stock"] = stock
        return super().create(validated_data)


class StockRecommendationSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="stock.symbol", read_only=True)
    name = serializers.CharField(source="stock.name", read_only=True)

    class Meta:
        model = StockRecommendation
        fields = "__all__"


class IPOListingSerializer(serializers.ModelSerializer):
    class Meta:
        model = IPOListing
        fields = "__all__"


class IndexConstituentSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="stock.symbol", read_only=True)
    name = serializers.CharField(source="stock.name", read_only=True)

    class Meta:
        model = IndexConstituent
        fields = ("id", "stock", "symbol", "name", "weight_pct", "last_price", "change_pct", "updated_at")


class IndexSerializer(serializers.ModelSerializer):
    """
    Includes the index's own latest price (from IndexPriceSnapshot,
    apps.investing.tasks.sync_index_constituents_and_prices' own
    output) inline -- the dashboard's ticker cards need "NIFTY 50:
    24,850 (+0.4%)" in one call, not a second request per card.
    """
    latest_price = serializers.SerializerMethodField()
    constituent_count = serializers.SerializerMethodField()

    class Meta:
        model = Index
        fields = ("id", "name", "symbol", "exchange", "latest_price", "constituent_count")

    def get_latest_price(self, obj):
        latest = obj.price_snapshots.order_by("-timestamp").first()
        if latest is None:
            return None
        # ltp/change are DecimalField -- DRF's JSON encoder serializes a
        # raw Decimal as a STRING, unlike every other price field in this
        # codebase (apps.options.views, apps.options.signals, etc.), which
        # all explicitly float()-cast before returning. Left un-cast here,
        # frontend code that does `spot.toFixed(...)` on this value (a
        # Number-only method) would throw -- and with no React error
        # boundary in place (see RouteErrorBoundary.jsx), an uncaught
        # render exception like that used to take down the whole app,
        # nav bars included, not just the one page.
        return {
            "ltp": float(latest.ltp),
            "change": float(latest.change) if latest.change is not None else None,
            "change_pct": latest.change_pct,
            "timestamp": latest.timestamp,
        }

    def get_constituent_count(self, obj):
        return obj.constituents.count()
