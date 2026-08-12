from datetime import date

from django.utils import timezone
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import metrics
from .greeks import compute_greeks_for_contract
from .models import OptionChainSnapshot, OptionContract
from .serializers import OptionChainSnapshotSerializer, OptionContractSerializer
from .signals_engine import _latest_underlying_ltp, evaluate_options_signals


class OptionContractViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OptionContract.objects.all()
    serializer_class = OptionContractSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["underlying", "expiry", "option_type"]


class OptionChainSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OptionChainSnapshot.objects.select_related("contract").all()
    serializer_class = OptionChainSnapshotSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["contract__underlying", "contract__expiry"]


class OptionExpiriesView(APIView):
    """
    Distinct CURRENT-OR-FUTURE expiries already synced into
    OptionContract for one underlying (via `python manage.py
    sync_option_contracts`), nearest first -- backs the frontend's
    expiry dropdown so it only ever offers a date that actually has
    contracts to query, instead of a blind date-picker that 404s on a
    not-yet-synced expiry.

    expiry__gte filters out already-expired rows -- without it, a
    past expiry that's still in the DB (this app deliberately never
    deletes expired OptionContract rows, so historical
    OptionChainSnapshot data stays available for backtesting/analytics,
    see OptionContract's own docstring) would sort first and get
    offered as a live choice. The frontend (OptionsAnalytics.jsx)
    auto-selects this list's FIRST entry as the default expiry on load,
    so an unfiltered list here meant the page defaulted to showing a
    dead, expired option chain -- same underlying bug apps.options.
    signals_engine.nearest_expiry() had, fixed there for the same reason.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        underlying = request.query_params.get("underlying", "NIFTY")
        expiries = (
            OptionContract.objects.filter(underlying=underlying, expiry__gte=timezone.localdate())
            .order_by("expiry").values_list("expiry", flat=True).distinct()
        )
        return Response({"underlying": underlying, "expiries": [e.isoformat() for e in expiries]})


class OptionChainView(APIView):
    """
    The actual option-chain grid for one underlying+expiry: every
    strike with its CE and PE side-by-side (LTP, OI, change in OI,
    volume), each from that contract's latest snapshot -- what the
    frontend's Option Chain table renders directly, one row per strike,
    instead of the frontend having to reconstruct that shape itself
    from the flatter OptionContractViewSet/OptionChainSnapshotViewSet
    list endpoints.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        underlying = request.query_params.get("underlying", "NIFTY")
        expiry_str = request.query_params.get("expiry")
        if not expiry_str:
            return Response({"error": "expiry query param (YYYY-MM-DD) is required."}, status=400)
        expiry = date.fromisoformat(expiry_str)

        contracts = OptionContract.objects.filter(underlying=underlying, expiry=expiry)
        latest_by_contract = {}
        for snapshot in (
            OptionChainSnapshot.objects.filter(contract__in=contracts)
            .select_related("contract").order_by("contract_id", "-timestamp")
        ):
            # order_by + first-seen-per-contract-id gives the latest
            # snapshot per contract without an extra query per contract
            # (which a naive "for c in contracts: c.snapshots.first()"
            # loop would do -- N+1 queries for an 80+ contract expiry).
            latest_by_contract.setdefault(snapshot.contract_id, snapshot)

        spot = _latest_underlying_ltp(underlying, expiry)

        rows: dict[float, dict] = {}
        for contract in contracts:
            snapshot = latest_by_contract.get(contract.id)
            side = "call" if contract.option_type == "CE" else "put"
            row = rows.setdefault(float(contract.strike), {"strike": float(contract.strike), "call": None, "put": None})
            if snapshot is None:
                continue
            leg = {
                "ltp": float(snapshot.ltp),
                "open_interest": snapshot.open_interest,
                "change_in_oi": snapshot.change_in_oi,
                "volume": snapshot.volume,
                "iv": snapshot.iv,
                "bid": float(snapshot.bid) if snapshot.bid is not None else None,
                "ask": float(snapshot.ask) if snapshot.ask is not None else None,
                "timestamp": snapshot.timestamp.isoformat(),
            }
            # Greeks computed on the fly from the stored snapshot's own
            # IV (see apps.options.greeks) -- not persisted, since
            # they're cheap to compute and change with every snapshot
            # anyway (spot price moves every tick).
            if spot is not None and snapshot.iv is not None:
                greeks = compute_greeks_for_contract(contract, spot, float(snapshot.ltp))
                if greeks is not None:
                    leg["greeks"] = {k: v for k, v in greeks.items() if k != "iv"}
            row[side] = leg

        return Response({
            "underlying": underlying,
            "expiry": expiry_str,
            "spot": spot,
            "rows": sorted(rows.values(), key=lambda r: r["strike"]),
        })


class OptionsAnalyticsView(APIView):
    """
    Single endpoint bundling PCR, max pain, strike-wise support/
    resistance, and all the manual-section-9 options signals for one
    underlying+expiry -- the frontend's Options Analytics page
    (manual section 6) is meant to call this once rather than stitching
    together several separate metric endpoints itself.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        underlying = request.query_params.get("underlying", "NIFTY")
        expiry_str = request.query_params.get("expiry")
        if not expiry_str:
            return Response({"error": "expiry query param (YYYY-MM-DD) is required."}, status=400)
        expiry = date.fromisoformat(expiry_str)

        return Response({
            "underlying": underlying,
            "expiry": expiry_str,
            "pcr": metrics.compute_pcr(underlying, expiry),
            "max_pain": metrics.compute_max_pain(underlying, expiry),
            "support_resistance": metrics.strike_support_resistance(underlying, expiry),
            "signals": evaluate_options_signals(underlying, expiry),
        })


class BestStrikeView(APIView):
    """
    manual intro (Problem 7 "Complex Option Selection"): "AI analyzes
    Probability, OI, Greeks, Volume, Risk and suggests the Best
    Strike." See apps.options.strike_selector.suggest_best_strike for
    the actual scoring.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        underlying = request.query_params.get("underlying", "NIFTY")
        expiry_str = request.query_params.get("expiry")
        direction = request.query_params.get("direction", "bullish")
        if not expiry_str:
            return Response({"error": "expiry query param (YYYY-MM-DD) is required."}, status=400)
        if direction not in ("bullish", "bearish"):
            return Response({"error": "direction must be 'bullish' or 'bearish'."}, status=400)
        expiry = date.fromisoformat(expiry_str)

        from .strike_selector import suggest_best_strike

        result = suggest_best_strike(underlying, expiry, direction)
        return Response({
            "underlying": underlying, "expiry": expiry_str, "direction": direction, **result,
        })
