# Delivery trading workspace

Open `/delivery` for stock discovery, saved watchlist, paper holdings and orders.
Authenticated APIs under `/api/investing/delivery/`: `stocks/`, `account/`, and `orders/`.
All data is scoped to the authenticated trader. The catalog comes from the existing Stock records.

Each trader has an independent paper ledger initialized with INR 100,000, separate from options account equity. Whole-share buys consume cash; sells cannot exceed holdings. Partial sells retain the average acquisition price and accumulate realized profit. The same UUID and payload can be retried without duplicate execution. The active kill switch blocks new buys while allowing sells.

Pricing uses the latest stored daily close no more than seven calendar days old. The reference date is returned with every order. This is a reference-price simulator, not a market-fill simulator. Fees and slippage are not included. No deposits, withdrawals, live equity orders or broker holdings reconciliation are provided by these APIs.

Live delivery requires broker equity routing, exchange sessions, current bid/ask feeds, fees, reconciliation and settlement accounting. Advanced GTT, limit orders and baskets remain outside the currently supported execution flow. Existing option trading remains available at `/option-trading`.
