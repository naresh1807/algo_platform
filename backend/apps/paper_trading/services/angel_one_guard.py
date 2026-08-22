"""
Fail-closed enforcement that this entire app can never route an order
to Angel One, layered on top of (not instead of) the existing guard in
apps.market_data.broker_client.BrokerClient.place_order/cancel_order
(raises PermissionError unless settings.LIVE_TRADING_ENABLED AND
settings.ALLOW_LIVE_ORDERS are both true -- see that module's own
comment; ALLOW_LIVE_ORDERS is the second, independent flag this app's
config always keeps false).

Two independent defenses, deliberately not one:

1. STRUCTURAL (this module, assert_no_broker_import): apps.paper_trading
   never imports apps.market_data.broker_client at all -- there is
   simply no code path here that COULD place a broker order, broker
   connectivity issues aside. assert_no_broker_import() statically scans
   every .py file in this app's own package (excluding test files, which
   legitimately reference broker_client's dotted path as a string to
   mock it) for the broker client's import path and its order-method
   names, and raises django.core.exceptions.ImproperlyConfigured --
   loudly, at Django startup (apps.py's AppConfig.ready calls this) --
   if any of them ever appear. A future accidental `from apps.market_data
   import broker_client` anywhere in this app fails the whole process to
   start, rather than silently existing until it happens to run.

2. CONFIGURATION (broker_client.py itself): even if some future code
   elsewhere in the platform tried to call BrokerClient.place_order on
   this app's behalf, `ALLOW_LIVE_ORDERS=false` (this app's own settings
   block, config/settings.py) makes that call raise PermissionError
   regardless of LIVE_TRADING_ENABLED -- and that guard also now writes
   an audit-log security event (apps.admin_tools.audit.log_action,
   action="blocked_live_order_attempt") every time it trips, for any
   caller in the codebase, not just this app.

apps/paper_trading/tests_broker_isolation.py additionally *empirically*
proves defense #1 by mocking BrokerClient.place_order/cancel_order and
asserting they are never called across a full simulated autonomous
cycle -- belt, suspenders, and a smoke test.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# Only matches actual code, never prose: an import line naming the
# ORDER-CAPABLE broker client module specifically (apps.market_data.
# broker_client -- NOT apps.options.broker_client / apps.options.pricing,
# which are legitimate read-only quote clients this app IS allowed to
# use for market data and have no order-placement methods at all), or an
# actual call (name followed by an opening paren) to one of the order-
# placement methods/functions. Deliberately NOT a bare word match --
# this module's own docstrings (and models.py's) need to be able to
# talk ABOUT broker_client/place_order in prose without tripping the
# scan; only a real `from apps.market_data import broker_client` or a
# real `x.place_order(...)` call is a genuine violation.
_FORBIDDEN_PATTERNS = [
    re.compile(r"^\s*(from|import)\s+.*\bmarket_data\.broker_client\b", re.MULTILINE),
    re.compile(r"^\s*(from|import)\s+.*\bmarket_data\s+import\s+.*\bbroker_client\b", re.MULTILINE),
    re.compile(r"\bget_broker_client\s*\("),
    re.compile(r"\bplace_order\s*\("),
    re.compile(r"\bplaceOrder\w*\s*\("),
    re.compile(r"\bmodifyOrder\s*\("),
    re.compile(r"\bcancelOrder\s*\("),
    re.compile(r"\bcancel_order\s*\("),
]

# Files allowed to mention the above -- test files legitimately patch
# `"apps.market_data.broker_client.BrokerClient.place_order"` as a
# string to assert it's never called, and this guard module itself
# documents the forbidden names above.
_EXEMPT_FILENAME_PREFIXES = ("test_", "tests_")
_EXEMPT_FILENAMES = {"angel_one_guard.py"}


class LiveOrderBlockedError(Exception):
    """
    Raised if any code path in this app ever attempts to act as though
    it could route an order to a real broker. Never expected to be
    raised in practice -- assert_no_broker_import() is meant to catch
    the mistake at import time, before this would ever fire -- but
    kept as an explicit, named exception (rather than a bare assert)
    so any future defensive call site has something specific to catch
    and to record as a apps.paper_trading.models.PaperRiskEvent
    (event_type="blocked_live_order_attempt", severity="critical").
    """


def assert_no_broker_import() -> None:
    """
    Called once from PaperTradingConfig.ready(). Scans every .py file
    under this app's own package for the forbidden patterns above and
    raises ImproperlyConfigured (which aborts Django startup entirely)
    if any are found outside an exempt (test) file.
    """
    app_dir = Path(__file__).resolve().parent.parent
    violations: list[str] = []

    for path in app_dir.rglob("*.py"):
        if path.name in _EXEMPT_FILENAMES or path.name.startswith(_EXEMPT_FILENAME_PREFIXES):
            continue
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(text):
                violations.append(f"{path.relative_to(app_dir.parent.parent)}: matched {pattern.pattern!r}")

    if violations:
        raise ImproperlyConfigured(
            "apps.paper_trading must never reference Angel One order-placement "
            "methods or the broker client -- this app is market-data-read-only, "
            "paper mode's fail-closed architecture depends on it structurally "
            "never importing that code. Violations found:\n" + "\n".join(violations)
        )
