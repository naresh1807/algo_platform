// Mirrors apps/market_data/market_hours.py's real, enforced constants
// (MARKET_OPEN_TIME=09:15, MARKET_CLOSE_TIME=15:30 IST, Mon-Fri) -- no
// backend endpoint currently exposes this as structured JSON, so it's
// computed here the same way the rest of this frontend already computes
// IST-local timestamps client-side (see constants/chartTime.js). NOT
// exchange-holiday-aware -- neither is the backend's own current check.
// Shared by MarketStatus.jsx (header badge) and anything that needs to
// gate "is a live tick expected right now" (e.g. chart staleness).
const OPEN_MINUTES = 9 * 60 + 15;
const CLOSE_MINUTES = 15 * 60 + 30;

export function marketOpenStatus(now = new Date()) {
  const istString = now.toLocaleString("en-US", { timeZone: "Asia/Kolkata", hour12: false });
  const ist = new Date(istString);
  const day = ist.getDay(); // 0=Sun, 6=Sat
  const minutes = ist.getHours() * 60 + ist.getMinutes();

  if (day === 0 || day === 6) return { open: false, detail: "Weekend" };
  if (minutes < OPEN_MINUTES) return { open: false, detail: "Opens 09:15 IST" };
  if (minutes >= CLOSE_MINUTES) return { open: false, detail: "Closed for the day" };
  return { open: true, detail: "09:15–15:30 IST" };
}

export function isMarketOpenNow() {
  return marketOpenStatus().open;
}
