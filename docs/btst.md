# BTST signals

The Celery Beat job `generate-btst-1430-ist` schedules `apps.investing.btst.generate_btst_signals` at 14:30 Asia/Kolkata Monday through Friday on the priority queue. The task checks the NSE holiday/session calendar and runs only from 14:30 to before 14:40 IST. Queue expiry is ten minutes. It cannot backfill a late scan with post-cutoff data. Worker and Beat must remain running; the timestamp is a scheduled dispatch time, not a guaranteed completion time.

Universe: up to 50 unique NSE equity stocks on any user's stock watchlist, ordered by symbol. Each user sees signals for their own watchlisted stocks at `/btst`. Add stocks through `/delivery`.

Trend-breakout BTST: prior daily EMA20 above EMA50, reference price above both and the prior twenty-day high, intraday position in the upper 20% of its range, and volume at least 1.2 times prorated prior twenty-day average. At least fifty daily bars and 280 current-session one-minute bars are required. Daily history excludes today. Latest intraday bar must close at or before 14:30 and be fresh. Missing data generates no signal.

Stop: tighter of intraday low and one ATR below reference, maximum 3% distance. Target: 1.5R. Exit date: next regular NSE trading session, skipping holidays and weekends. These are signal reference levels only; no broker order or automated next-session exit is scheduled. Overnight gaps may cross stops. Signal rules are unvalidated and are independent of the trained scalping models.

One durable run per date and one signal per stock/run prevent duplicates. Run status and symbol-specific reasons are persisted. Signals cannot bypass intraday square-off because they are stored in separate BTST tables, not execution positions. The calendar is verified for 2026 only; the task skips unsupported years and next-session dates crossing into them.

Calendar source: https://www.nseindia.com/resources/exchange-communication-holidays (equities, verified 6 October 2026). Seven BTST checks passed. Today's 14:30 window is past; first normal future dispatch is 7 October 2026 at 14:30 IST if services remain running.


## Additional setups

The same 14:30 scan now evaluates four setups. All require price above prior EMA20 above EMA50, volume at least 1.2 times the prorated prior 20-day average, and price in the upper 20% of today's range:

- **20-day trend breakout:** price above the prior 20-day high (existing rule).
- **Inside-day breakout:** yesterday's high is strictly below the previous day's high and its low strictly above the previous day's low; today's reference breaks the previous day's high.
- **EMA20 pullback recovery:** today's low touches the band from EMA20 minus 0.5 ATR to EMA20 plus 0.25 ATR, then reference recovers above yesterday's high and today's open.
- **Opening-range breakout above VWAP:** reference exceeds the first 30 completed one-minute bars' high, today's volume-weighted typical price, and yesterday's high.

Every setup uses the existing maximum 3% stop distance and 1.5R target. Matching setup IDs and indicator evidence are saved in `evidence`; the API returns their display names. A stock qualifying under multiple rules produces one signal listing all matches, with a deterministic primary strategy in the order above. Existing signals display as the original trend-breakout strategy. These are rule-based, unvalidated signals, not a profitability claim or trained-model recommendation. No automatic orders are placed.
