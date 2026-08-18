from datetime import date

from django.core.cache import cache
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import metrics
from .greeks import compute_greeks_for_contract
from .models import OptionChainSnapshot, OptionContract, OptionsStrategySetting
from .serializers import OptionChainSnapshotSerializer, OptionContractSerializer, OptionsStrategySettingSerializer
from .signals_engine import _latest_underlying_ltp, evaluate_options_signals


class OptionContractViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OptionContract.objects.all()
    serializer_class = OptionContractSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["underlying", "expiry", "option_type"]


class OptionChainSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """
    contract__strike/contract__option_type added alongside the
    original underlying/expiry filters so the frontend can ask for one
    exact contract's own snapshot history (?contract__underlying=NIFTY
    &contract__expiry=2026-08-18&contract__strike=24000&
    contract__option_type=CE) -- the Options Analytics page's chain-
    row click-to-chart popup uses exactly this to plot that contract's
    LTP over time, the option-chain equivalent of clicking a stock to
    see its price chart. Model's own Meta.ordering (-timestamp) means
    results already come back newest-first without an extra param.
    """
    queryset = OptionChainSnapshot.objects.select_related("contract").all()
    serializer_class = OptionChainSnapshotSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["contract__underlying", "contract__expiry", "contract__strike", "contract__option_type"]


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
        # metrics._latest_snapshots already does exactly this -- one
        # row per contract, fetched via a window function -- so this
        # view reuses it instead of its own "order_by + first-seen-per-
        # contract-id" copy, which although N+1-query-safe, still
        # pulled EVERY historical snapshot ever recorded for these
        # contracts over the wire just to keep the first one seen: on
        # this table's real, ever-growing ingestion history that was
        # transferring far more rows than needed on every single
        # request, and only gets slower as more snapshots accumulate.
        latest_by_contract = {
            snapshot.contract_id: snapshot for snapshot in metrics._latest_snapshots(underlying, expiry)
        }

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

        # Fetched once and reused across the three calls below --
        # compute_pcr/compute_max_pain/strike_support_resistance each
        # independently ran this same correlated-subquery fetch before,
        # tripling it for no reason on every call to this view.
        snapshots = list(metrics._latest_snapshots(underlying, expiry))

        return Response({
            "underlying": underlying,
            "expiry": expiry_str,
            "pcr": metrics.compute_pcr(underlying, expiry, snapshots=snapshots),
            "max_pain": metrics.compute_max_pain(underlying, expiry, snapshots=snapshots),
            "support_resistance": metrics.strike_support_resistance(underlying, expiry, snapshots=snapshots),
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


class OptionsStrategySettingView(APIView):
    """
    GET/POST for apps.options.models.OptionsStrategySetting -- the
    expiry/strike mode preferences apps.options.index_direction_strategy
    reads via select_expiry()/suggest_best_strike() on every scheduled
    evaluation. Same DB-backed-singleton pattern as apps.execution.
    views.ExecutionModeView, minus that view's LIVE-mode confirmation
    phrase -- changing a strategy preference isn't a real-money-risk
    action the way flipping to live execution is, so no extra friction.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        row, _ = OptionsStrategySetting.objects.get_or_create(pk=1)
        return Response(OptionsStrategySettingSerializer(row).data)

    def post(self, request):
        row, _ = OptionsStrategySetting.objects.get_or_create(pk=1)
        serializer = OptionsStrategySettingSerializer(row, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save(changed_by=request.user.get_username())
        return Response(serializer.data)


EVALUATE_NOW_COOLDOWN_SECONDS = 30  # per-underlying throttle -- evaluate_index_direction_trade
# makes live broker calls (spot LTP, option chain quotes) inline in the
# request/response cycle, and this codebase has real prior AB1021
# rate-limit-cooldown incident history (see project memory) -- a user
# mashing a "Re-analyze Now" button must not be able to burst past that
# on top of the existing 5-minute beat cadence.


class EvaluateNowView(APIView):
    """
    Manual trigger for apps.options.index_direction_strategy.
    evaluate_index_direction_trade -- backs the frontend terminal's
    "Re-analyze Now" button so a trader doesn't have to wait up to 5
    minutes for the next scheduled beat tick. Thin wrapper only: all the
    real pipeline logic stays in index_direction_strategy, this view
    just calls it synchronously and returns the resulting signal.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        underlying = request.data.get("underlying", "NIFTY")
        timeframe = request.data.get("timeframe", "5m")

        cache_key = f"options:evaluate_now:{underlying}"
        if cache.get(cache_key):
            return Response(
                {"error": f"Please wait a moment before re-analyzing {underlying} again."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        cache.set(cache_key, True, timeout=EVALUATE_NOW_COOLDOWN_SECONDS)

        from apps.signals.serializers import TradingSignalSerializer

        from .index_direction_strategy import evaluate_index_direction_trade

        signal = evaluate_index_direction_trade(underlying, timeframe)
        return Response(TradingSignalSerializer(signal).data)


class FinalSignalView(APIView):
    """
    GET /api/options/final-signal/?underlying=NIFTY -- apps.options.
    final_signal.resolve_final_signal for the LATEST TradingSignal on
    this underlying, assembled into this platform's full structured
    "final signal" shape (exact contract identity, greeks, support/
    resistance, expected move, scored confirmation factors, itemized
    explanation, strategy classification). Read-only view builder --
    never re-runs the trading decision itself; see that module's own
    docstring.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        underlying = request.query_params.get("underlying", "NIFTY")

        from apps.signals.models import TradingSignal

        from .final_signal import resolve_final_signal

        signal = (
            TradingSignal.objects.filter(symbol=underlying).select_related("option_contract")
            .order_by("-created_at").first()
        )
        if signal is None:
            return Response({"error": f"No signals generated yet for {underlying}."}, status=status.HTTP_404_NOT_FOUND)

        return Response(resolve_final_signal(signal))
