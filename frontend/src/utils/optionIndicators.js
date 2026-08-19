/**
 * Pure, client-side indicator calculations for an OPTION CONTRACT's own
 * candles -- deliberately NOT computed server-side the way
 * apps.market_data.indicators computes them for the underlying's chart.
 * That module's whole design assumes indicators only need to change
 * once per REST fetch (symbol/timeframe change); this feature's own
 * requirement is that indicators re-derive on every live tick and on
 * every candle close, which a server round-trip per tick isn't a fit
 * for. Every function below takes a plain, ascending candles array
 * ({time, open, high, low, close, volume}) and returns a NEW array --
 * the input is never mutated -- with `null` (never 0) for any bar still
 * inside its own warm-up period.
 */

function toNum(v) {
  return v == null ? null : Number(v);
}

// Standard EMA: seeded with a simple average of the first `period`
// closes (the usual charting-platform warm-up), then smoothed forward.
function emaSeries(candles, period) {
  const closes = candles.map((c) => toNum(c.close));
  const out = new Array(candles.length).fill(null);
  if (candles.length < period) return out;
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  out[period - 1] = ema;
  const k = 2 / (period + 1);
  for (let i = period; i < candles.length; i++) {
    ema = closes[i] * k + ema * (1 - k);
    out[i] = ema;
  }
  return out;
}

export function computeEMA(candles, period) {
  return emaSeries(candles, period).map((value, i) => ({ time: candles[i].time, value }));
}

// Cumulative typical-price VWAP, resetting at each IST calendar-day
// boundary (an option contract's VWAP is meaningless carried across
// session boundaries the way a running average would otherwise do).
export function computeVWAP(candles) {
  let cumPV = 0;
  let cumVol = 0;
  let lastDay = null;
  return candles.map((c) => {
    const day = new Date(c.time).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
    if (day !== lastDay) {
      cumPV = 0;
      cumVol = 0;
      lastDay = day;
    }
    const typicalPrice = (toNum(c.high) + toNum(c.low) + toNum(c.close)) / 3;
    const vol = toNum(c.volume) || 0;
    cumPV += typicalPrice * vol;
    cumVol += vol;
    return { time: c.time, value: cumVol > 0 ? cumPV / cumVol : null };
  });
}

// Wilder-smoothed RSI(14) -- same formula as apps.market_data.indicators
// ._rsi, reimplemented over a plain array here since that module works
// against a pandas Series server-side.
export function computeRSI(candles, period = 14) {
  const n = candles.length;
  const out = new Array(n).fill(null);
  if (n < period + 1) return candles.map((c) => ({ time: c.time, rsi: null }));

  let avgGain = 0;
  let avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const change = toNum(candles[i].close) - toNum(candles[i - 1].close);
    avgGain += Math.max(change, 0);
    avgLoss += Math.max(-change, 0);
  }
  avgGain /= period;
  avgLoss /= period;
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);

  for (let i = period + 1; i < n; i++) {
    const change = toNum(candles[i].close) - toNum(candles[i - 1].close);
    const gain = Math.max(change, 0);
    const loss = Math.max(-change, 0);
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return candles.map((c, i) => ({ time: c.time, rsi: out[i] }));
}

// MACD(12,26,9) -- macd/signal/histogram, matching the field names
// apps.market_data.indicators.compute_indicator_series already uses so
// the existing MACDPanel.jsx component can render this unchanged.
export function computeMACD(candles) {
  const ema12 = emaSeries(candles, 12);
  const ema26 = emaSeries(candles, 26);
  const macdLine = candles.map((c, i) => (ema12[i] != null && ema26[i] != null ? ema12[i] - ema26[i] : null));

  const firstValidIdx = macdLine.findIndex((v) => v !== null);
  const signalLine = new Array(candles.length).fill(null);
  if (firstValidIdx !== -1) {
    const k = 2 / (9 + 1);
    let signal = null;
    for (let i = firstValidIdx; i < candles.length; i++) {
      if (macdLine[i] === null) continue;
      signal = signal === null ? macdLine[i] : macdLine[i] * k + signal * (1 - k);
      signalLine[i] = signal;
    }
  }

  return candles.map((c, i) => ({
    time: c.time,
    macd: macdLine[i],
    macd_signal: signalLine[i],
    macd_hist: macdLine[i] != null && signalLine[i] != null ? macdLine[i] - signalLine[i] : null,
  }));
}

// Standard ATR(period)-based Supertrend -- returns {time, value,
// direction: "up"|"down"|null}. `value` is the trailing stop level
// itself (plotted as the overlay line); `direction` is the standard
// bull/bear flip a caller could use for a color change if desired.
export function computeSupertrend(candles, period = 10, multiplier = 3) {
  const n = candles.length;
  if (n < period + 1) return candles.map((c) => ({ time: c.time, value: null, direction: null }));

  const tr = candles.map((c, i) => {
    const high = toNum(c.high);
    const low = toNum(c.low);
    if (i === 0) return high - low;
    const prevClose = toNum(candles[i - 1].close);
    return Math.max(high - low, Math.abs(high - prevClose), Math.abs(low - prevClose));
  });

  const atr = new Array(n).fill(null);
  let sum = 0;
  for (let i = 0; i < period; i++) sum += tr[i];
  atr[period - 1] = sum / period;
  for (let i = period; i < n; i++) {
    atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period;
  }

  const out = new Array(n).fill(null);
  let finalUpper = null;
  let finalLower = null;
  let trendUp = true;
  for (let i = period - 1; i < n; i++) {
    if (atr[i] == null) continue;
    const high = toNum(candles[i].high);
    const low = toNum(candles[i].low);
    const close = toNum(candles[i].close);
    const hl2 = (high + low) / 2;
    const basicUpper = hl2 + multiplier * atr[i];
    const basicLower = hl2 - multiplier * atr[i];
    const prevClose = i > 0 ? toNum(candles[i - 1].close) : close;

    if (finalUpper === null) {
      finalUpper = basicUpper;
      finalLower = basicLower;
    } else {
      finalUpper = basicUpper < finalUpper || prevClose > finalUpper ? basicUpper : finalUpper;
      finalLower = basicLower > finalLower || prevClose < finalLower ? basicLower : finalLower;
    }

    if (close > finalUpper) trendUp = true;
    else if (close < finalLower) trendUp = false;
    // else: trend direction unchanged -- the standard Supertrend rule.

    out[i] = { value: trendUp ? finalLower : finalUpper, direction: trendUp ? "up" : "down" };
  }
  return candles.map((c, i) => ({ time: c.time, value: out[i]?.value ?? null, direction: out[i]?.direction ?? null }));
}

/**
 * One combined pass producing the exact row shape RSIPanel.jsx/
 * MACDPanel.jsx already expect ({timestamp, rsi, macd, macd_signal,
 * macd_hist}), plus ema9/ema21 -- so those two existing chart
 * components can be reused unchanged for an option contract's own
 * candles, not reimplemented. VWAP/Supertrend are returned separately
 * (computeVWAP/computeSupertrend above) since they're overlays on the
 * main candlestick pane, not a lower panel.
 */
export function computeOptionIndicators(candles) {
  const ema9 = emaSeries(candles, 9);
  const ema21 = emaSeries(candles, 21);
  const rsi = computeRSI(candles);
  const macd = computeMACD(candles);
  return candles.map((c, i) => ({
    timestamp: c.time,
    ema9: ema9[i],
    ema21: ema21[i],
    rsi: rsi[i].rsi,
    macd: macd[i].macd,
    macd_signal: macd[i].macd_signal,
    macd_hist: macd[i].macd_hist,
  }));
}
