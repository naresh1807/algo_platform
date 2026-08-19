from datetime import date, datetime

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsAdminGroup, IsTraderOrAdmin

from apps.admin_tools.audit import log_action

from . import metrics
from .greeks import compute_greeks_for_contract
from .models import OptionChainSnapshot, OptionContract, OptionsStrategySetting
from .serializers import OptionChainSnapshotSerializer, OptionContractSerializer, OptionsStrategySettingSerializer
from .signals_engine import _latest_underlying_ltp, evaluate_options_signals


class OptionContractViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Defaults to is_active=True (excludes expired/rolled-over contracts)
    -- this is the "live-trading APIs must not return expired contracts
    by default" requirement. Historical/backtesting callers that need
    expired rows too (they're never deleted, see OptionContract's own
    docstring) can still get them explicitly via ?is_active=false or
    ?is_active=true (django-filter's BooleanFilter), which bypasses the
    default below.
    """
    queryset = OptionContract.objects.all()
    serializer_class = OptionContractSerializer
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["underlying", "expiry", "option_type", "is_active"]

    def get_queryset(self):
        qs = super().get_queryset()
        if "is_active" not in self.request.query_params:
            qs = qs.filter(is_active=True)
        return qs


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
    permission_classes = [IsTraderOrAdmin]
    filterset_fields = ["contract__underlying", "contract__expiry", "contract__strike", "contract__option_type"]


class OptionExpiriesView(APIView):
    """
    Cutoff-eligible expiries already synced into OptionContract for one
    underlying (via `python manage.py sync_option_contracts`), nearest
    first -- backs the frontend's expiry dropdown so it only ever offers
    a date that actually has contracts to query, instead of a blind
    date-picker that 404s on a not-yet-synced expiry.

    Delegates entirely to apps.options.expiry_service.list_eligible_expiries
    now (kept as its own view/response-shape for backward compatibility
    with existing callers) -- this used to carry its own
    `expiry__gte=today` filter, the same bug apps.options.signals_engine.
    nearest_expiry() had (see that function's docstring): a pure date
    comparison stays True all day on expiry day itself, hours after that
    contract's own tradeable life ended at market close. New code should
    prefer OptionExpiryStatusView below, which also reports
    current_expiry/next_expiry/sync health in one call.
    """
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from .expiry_service import list_eligible_expiries

        underlying = request.query_params.get("underlying", "NIFTY")
        expiries = list_eligible_expiries(underlying)
        return Response({"underlying": underlying, "expiries": [e.isoformat() for e in expiries]})


class OptionExpiryStatusView(APIView):
    """
    GET /api/options/expiry-status/?underlying=NIFTY -- the single call
    the frontend needs for the full expiry-lifecycle picture: which
    expiry is current, which rolls in next, every eligible expiry to
    offer in a dropdown, when contract sync last actually succeeded, and
    whether a rollover is currently overdue. Entirely backed by
    apps.options.expiry_service.expiry_status -- this view contains no
    expiry-selection logic of its own.

    Returns 503 (not 200 with an empty/misleading body) when there is no
    valid current expiry AND rollover_required is true -- i.e. sync
    genuinely cannot serve real contracts right now -- so the frontend
    can show an honest "temporarily unavailable" state instead of
    silently rendering nothing or, worse, an expired chain.
    """
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from .expiry_service import expiry_status

        underlying = request.query_params.get("underlying", "NIFTY")
        status_obj = expiry_status(underlying)
        body = status_obj.as_dict()
        if status_obj.current_expiry is None and status_obj.rollover_required:
            return Response(body, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(body)


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
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from .expiry_service import validate_requested_expiry

        underlying = request.query_params.get("underlying", "NIFTY")
        expiry_str = request.query_params.get("expiry")
        if not expiry_str:
            return Response({"error": "expiry query param (YYYY-MM-DD) is required."}, status=400)
        try:
            requested_expiry = date.fromisoformat(expiry_str)
        except ValueError:
            return Response({"error": f"expiry {expiry_str!r} is not a valid YYYY-MM-DD date."}, status=400)

        # Never silently serve a stale/expired chain just because the
        # caller asked for a date that used to be valid (e.g. a browser
        # tab left open since before today's rollover) -- falls back to
        # the real current expiry and reports the substitution instead.
        expiry, was_substituted = validate_requested_expiry(underlying, requested_expiry)
        if expiry is None:
            return Response(
                {"error": f"No valid (non-expired) expiry available for {underlying} right now.", "rollover_required": True},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        expiry_str = expiry.isoformat()

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
            "substituted_expiry": was_substituted,
            "spot": spot,
            "rows": sorted(rows.values(), key=lambda r: r["strike"]),
        })


def _parse_query_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(raw)
    if parsed is None:
        raise ValueError(f"{raw!r} is not a valid ISO-8601 datetime.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed


class OptionCandlesView(APIView):
    """
    GET /api/options/candles/?<identity>&timeframe=5m[&from=&to=]

    `<identity>` is one of:
      - contract=<OptionContract id>
      - token=<Angel One symbol_token>
      - underlying=&expiry=&strike=&option_type= (exactly what an
        option-chain row click already knows, before any token exists)

    Resolution happens entirely server-side (apps.options.candle_service
    .resolve_contract) -- the frontend never constructs or guesses a
    token/tradingsymbol itself, matching this app's "backend is the
    source of truth for contract identity" convention already
    established by apps.options.expiry_service/contract_sync for expiry
    selection. Adapted from apps.market_data.views.HistoricalDataViewSet's
    own symbol/timeframe query-param convention rather than a literal
    duplicate of it -- option candles are identified by an exact
    contract, never just an underlying string, so this is a plain
    APIView (one contract's own history, custom response shape) instead
    of a ViewSet/paginated list the way the underlying's candles are.

    404 (not 200-with-empty-list) when the requested identity doesn't
    resolve to a real, synced contract -- e.g. right after an expiry
    rollover, when a strike that existed on the old expiry has no
    equivalent row yet on the new one. The frontend hook
    (useOptionCandles.js) treats this as the trigger to fall back to the
    nearest still-listed strike on the same side, rather than silently
    showing an empty chart for a contract that no longer exists.
    """

    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        from .candle_service import (
            TIMEFRAME_CHOICES,
            ContractResolutionError,
            OPTION_EXCHANGE,
            get_option_candles,
            resolve_contract,
        )

        timeframe = request.query_params.get("timeframe", "5m")
        if timeframe not in TIMEFRAME_CHOICES:
            return Response(
                {"error": f"Unsupported timeframe {timeframe!r}. Choose one of: {sorted(TIMEFRAME_CHOICES)}."},
                status=400,
            )

        expiry = None
        expiry_str = request.query_params.get("expiry")
        if expiry_str:
            try:
                expiry = date.fromisoformat(expiry_str)
            except ValueError:
                return Response({"error": f"expiry {expiry_str!r} is not a valid YYYY-MM-DD date."}, status=400)

        try:
            contract = resolve_contract(
                contract_id=request.query_params.get("contract"),
                token=request.query_params.get("token"),
                underlying=request.query_params.get("underlying"),
                expiry=expiry,
                strike=request.query_params.get("strike"),
                option_type=request.query_params.get("option_type"),
            )
        except ContractResolutionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)

        try:
            from_dt = _parse_query_dt(request.query_params.get("from"))
            to_dt = _parse_query_dt(request.query_params.get("to"))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)

        candles = get_option_candles(contract, timeframe, from_dt=from_dt, to_dt=to_dt)

        return Response({
            "contract": {
                "id": contract.id,
                "token": contract.symbol_token,
                "trading_symbol": contract.tradingsymbol,
                "underlying": contract.underlying,
                "expiry": contract.expiry.isoformat(),
                "strike": float(contract.strike),
                "option_type": contract.option_type,
                "exchange": OPTION_EXCHANGE,
            },
            "timeframe": timeframe,
            "candles": candles,
        })


class OptionsAnalyticsView(APIView):
    """
    Single endpoint bundling PCR, max pain, strike-wise support/
    resistance, and all the manual-section-9 options signals for one
    underlying+expiry -- the frontend's Options Analytics page
    (manual section 6) is meant to call this once rather than stitching
    together several separate metric endpoints itself.
    """
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        underlying = request.query_params.get("underlying", "NIFTY")
        expiry_str = request.query_params.get("expiry")
        if not expiry_str:
            return Response({"error": "expiry query param (YYYY-MM-DD) is required."}, status=400)
        try:
            expiry = date.fromisoformat(expiry_str)
        except ValueError:
            return Response({"error": "expiry must use YYYY-MM-DD format."}, status=400)

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
    permission_classes = [IsTraderOrAdmin]

    def get(self, request):
        underlying = request.query_params.get("underlying", "NIFTY")
        expiry_str = request.query_params.get("expiry")
        direction = request.query_params.get("direction", "bullish")
        if not expiry_str:
            return Response({"error": "expiry query param (YYYY-MM-DD) is required."}, status=400)
        if direction not in ("bullish", "bearish"):
            return Response({"error": "direction must be 'bullish' or 'bearish'."}, status=400)
        try:
            expiry = date.fromisoformat(expiry_str)
        except ValueError:
            return Response({"error": "expiry must use YYYY-MM-DD format."}, status=400)

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
    views.ExecutionModeView. Reading is available to dashboard roles;
    changing a platform-wide strategy preference is an Admin-only,
    audited governance action.
    """

    permission_classes = [IsTraderOrAdmin]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminGroup()]
        return [IsTraderOrAdmin()]

    def get(self, request):
        # A GET must remain read-only.  Serialize an unsaved default when
        # this singleton has not been configured yet; the first durable
        # row is created only by the Admin-only POST below.
        row = OptionsStrategySetting.objects.filter(pk=1).first() or OptionsStrategySetting(pk=1)
        return Response(OptionsStrategySettingSerializer(row).data)

    def post(self, request):
        with transaction.atomic():
            row, _ = OptionsStrategySetting.objects.select_for_update().get_or_create(pk=1)
            serializer = OptionsStrategySettingSerializer(row, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            row = serializer.save(changed_by=request.user.get_username())

        log_action(
            action="options_strategy_settings_changed",
            actor=request.user,
            target=row,
            details={
                "expiry_mode": row.expiry_mode,
                "strike_mode": row.strike_mode,
                "itm_otm_steps": row.itm_otm_steps,
                "custom_expiry": row.custom_expiry.isoformat() if row.custom_expiry else None,
            },
        )
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

    # This creates a system TradingSignal that the execution loop may
    # consume in live mode.  It is therefore a platform-level trading
    # action, not an ordinary dashboard read or a personal watch-list
    # mutation.
    permission_classes = [IsAdminGroup]

    def post(self, request):
        from .index_direction_strategy import INDEX_UNDERLYINGS, evaluate_index_direction_trade

        underlying = str(request.data.get("underlying", "NIFTY")).strip().upper()
        timeframe = str(request.data.get("timeframe", "5m")).strip()
        if underlying not in INDEX_UNDERLYINGS:
            return Response(
                {"error": f"Unsupported underlying. Choose one of: {sorted(INDEX_UNDERLYINGS)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timeframe not in settings.CHART_TIMEFRAMES:
            return Response(
                {"error": f"Unsupported timeframe. Choose one of: {settings.CHART_TIMEFRAMES}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = f"options:evaluate_now:{underlying}"
        if cache.get(cache_key):
            return Response(
                {"error": f"Please wait a moment before re-analyzing {underlying} again."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        cache.set(cache_key, True, timeout=EVALUATE_NOW_COOLDOWN_SECONDS)

        from apps.signals.serializers import TradingSignalSerializer

        signal = evaluate_index_direction_trade(underlying, timeframe)
        log_action(
            action="options_evaluation_requested",
            actor=request.user,
            target=signal,
            details={"underlying": underlying, "timeframe": timeframe},
        )
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

    permission_classes = [IsTraderOrAdmin]

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
