"""
Shared test fixtures for apps.paper_trading's test suite. Builds real
candle history (enough for apps.market_data.indicators.compute_indicators's
MIN_CANDLES_REQUIRED) and a Black-Scholes-CONSISTENT synthetic option
chain -- apps.options.strike_selector.suggest_best_strike requires every
candidate contract's IV to actually solve (compute_greeks_for_contract
returns None otherwise, silently excluding it), so an arbitrary/fake
premium would make every contract_selection test fail for the wrong
reason. Premiums here are computed FROM the same black_scholes_price
function the IV solver inverts, guaranteeing a convergent solve.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

UNDERLYING = "NIFTY"
TIMEFRAME = "5m"
SPOT = 24500.0
SIGMA = 0.15
RATE = 0.065


def seed_underlying_candles(n: int = 90, base_price: float = SPOT, trend_per_bar: float = 0.0, timeframe: str = TIMEFRAME, symbol: str = UNDERLYING) -> float:
    from apps.market_data.models import HistoricalData

    now = timezone.now()
    price = base_price - trend_per_bar * n
    rows = []
    for i in range(n):
        ts = now - timedelta(minutes=5 * (n - i))
        open_p = price
        close_p = price + trend_per_bar
        high_p = max(open_p, close_p) + 2
        low_p = min(open_p, close_p) - 2
        rows.append(HistoricalData(
            symbol=symbol, timeframe=timeframe, timestamp=ts,
            open=Decimal(str(round(open_p, 2))), high=Decimal(str(round(high_p, 2))),
            low=Decimal(str(round(low_p, 2))), close=Decimal(str(round(close_p, 2))),
            volume=100000, source="test_fixture",
        ))
        price = close_p
    HistoricalData.objects.bulk_create(rows)
    return price


def seed_option_chain(spot: float = SPOT, expiry_offset_days: int = 3, sigma: float = SIGMA, underlying: str = UNDERLYING, oi: int = 5000, volume: int = 2000):
    from apps.options.greeks import black_scholes_price
    from apps.options.models import OptionChainSnapshot, OptionContract

    expiry = timezone.localdate() + timedelta(days=expiry_offset_days)
    tte_years = max(expiry_offset_days, 1) / 365.0
    strikes = [round((spot + step * 50) / 50) * 50 for step in range(-4, 5)]

    contracts = {}
    for strike in strikes:
        for option_type in ("CE", "PE"):
            contract, _ = OptionContract.objects.get_or_create(
                underlying=underlying, expiry=expiry, strike=Decimal(str(strike)), option_type=option_type,
                defaults=dict(
                    symbol_token=f"TEST{int(strike)}{option_type}{expiry_offset_days}",
                    tradingsymbol=f"{underlying}{option_type}{int(strike)}",
                    lot_size=75, is_active=True,
                ),
            )
            price = black_scholes_price(spot, strike, tte_years, RATE, sigma, option_type)
            price = max(price or 0.5, 0.5)
            bid = round(price * 0.99, 2)
            ask = round(price * 1.01, 2)
            OptionChainSnapshot.objects.create(
                contract=contract, timestamp=timezone.now(), ltp=Decimal(str(round(price, 2))),
                open_interest=oi, change_in_oi=0, volume=volume,
                iv=round(sigma * 100, 2), bid=Decimal(str(bid)), ask=Decimal(str(ask)),
            )
            contracts[(strike, option_type)] = contract
    return contracts, expiry


def seed_charge_rules() -> None:
    from .models import PaperChargeRuleSet

    PaperChargeRuleSet.objects.get_or_create(
        version="test_v1",
        defaults=dict(
            effective_from=date(2020, 1, 1), brokerage_flat_per_order=Decimal("20.00"),
            stt_pct_of_premium=Decimal("0.0625"), exchange_txn_pct=Decimal("0.03503"),
            gst_pct_of_brokerage_and_txn=Decimal("18.0"), sebi_charges_pct=Decimal("0.0001"),
            stamp_duty_pct_buy_side=Decimal("0.003"), other_charges_flat_per_order=Decimal("0"),
        ),
    )
