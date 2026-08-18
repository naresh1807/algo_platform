"""
Order Flow / Microstructure Engine -- EXPLICITLY UNAVAILABLE.

This platform has no tick-level trade feed and no market-depth/order-
book data source anywhere (confirmed: apps.market_data.broker_client
only exposes top-of-book LTP/OHLC quotes and apps.options.
OptionChainSnapshot stores bid/ask PRICE only, never bid/ask SIZE or
individual trade prints). Every function a real order-flow engine
would need -- trade direction, aggressive buying/selling, bid/ask size
imbalance, volume acceleration, price impact, trade intensity --
requires data this platform simply does not have.

Every function below returns a typed "unavailable" result rather than
either (a) silently omitting the capability, which would let a caller
iterating "every intelligence engine" miss that this one exists but
can't answer, or (b) faking a number from proxies that would look like
real order-flow data but isn't. If a real tick/depth feed is ever
wired into apps.market_data.broker_client, THIS is the module to
implement for real -- the function signatures below are the intended
extension points.
"""

from __future__ import annotations

UNAVAILABLE_REASON = (
    "No tick-level trade feed or market-depth/order-book data source exists in this platform "
    "(apps.market_data.broker_client exposes top-of-book LTP/OHLC only; apps.options."
    "OptionChainSnapshot stores bid/ask PRICE, not SIZE). Order-flow/microstructure analysis "
    "requires data this platform does not have -- never fabricated here."
)


def _unavailable(extra: str = "") -> dict:
    return {"available": False, "reason": UNAVAILABLE_REASON + (f" {extra}" if extra else "")}


def calculate_bid_ask_imbalance(*args, **kwargs) -> dict:
    return _unavailable("Needs bid/ask SIZE, not price.")


def detect_aggressive_buying(*args, **kwargs) -> dict:
    return _unavailable("Needs individual trade prints (trade side/aggressor flag).")


def detect_aggressive_selling(*args, **kwargs) -> dict:
    return _unavailable("Needs individual trade prints (trade side/aggressor flag).")


def calculate_volume_acceleration(*args, **kwargs) -> dict:
    return _unavailable("Needs sub-candle (tick-level) volume timestamps, not just candle-level totals.")


def detect_order_flow_shift(*args, **kwargs) -> dict:
    return _unavailable("Needs a time series of trade-level order-flow readings, none of which exist here.")


def detect_liquidity_withdrawal(*args, **kwargs) -> dict:
    return _unavailable("Needs market-depth (order book) snapshots over time to detect quotes being pulled.")
