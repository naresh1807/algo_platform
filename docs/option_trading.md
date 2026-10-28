# Option buying and selling

Open **Trading → Option Trading** (/option-trading). Select NIFTY or BANKNIFTY, an eligible expiry, and a CE/PE premium in the chain. Enter whole lots, a stop-loss premium below the buy premium, and a target above it. Preview the order, then buy. Both calls and puts open long positions in the selected option contract; prices are option premiums, not index points.

The page follows the existing Paper/Live execution mode. Paper is the default for a fresh installation. Live orders require the server's existing LIVE_TRADING_ENABLED gate, live mode, and per-order confirmation. Developing or testing this feature does not enable live trading.

**Sell to close** sells the full quantity of an existing long option position. It never opens a short option. Stops, targets, and configured trailing stops use the existing executors. A Celery job checks manually entered positions every 10 seconds during market hours, subject to worker availability. The general trading cycle and existing intraday square-off remain active. Stops are software monitored, not exchange-resident orders; quote gaps, feed outages, or worker delays can result in worse fills.

The backend requires complete contract metadata, an active expiry, quotes no older than 60 seconds, valid bid/ask, acceptable liquidity, and existing account risk checks. Requested quantity must fit the risk allowance. Paper fills retain modeled fees/slippage and may reduce quantity to respect the cost-inclusive risk budget.

Every order requires a UUID request_id. Repeating an identical request returns its existing result without resubmitting. Reusing that UUID for changed data or another user returns 409. A lost response should be retried with the same UUID. Unknown live orders require the existing broker reconciliation workflow; never submit a replacement order before resolving their state.

API endpoints:
- POST /api/execution/option-preview/ — contract_id, lots, stop_loss, target_price, execution_mode.
- POST /api/execution/option-orders/ — buy ticket plus action=buy and request_id; live adds confirm_live=true.
- POST /api/execution/option-orders/ — action=sell, position_id, execution_mode, request_id; live adds confirm_live=true.
- GET /api/execution/option-orders/ — the current user's most recent 100 requests.

Apply the additive execution migration before opening the page:
~~~powershell
.venv/Scripts/python.exe backend/manage.py migrate
~~~
Restart Django, Celery workers, and Celery Beat after updating. A working option feed and synced contracts are required. Frontend desktop/Android packages require rebuilding separately.

