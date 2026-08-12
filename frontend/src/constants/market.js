/**
 * Chart selector options. These must stay in sync with the backend:
 *   - SYMBOLS  <-> apps/market_data/broker_client.py SYMBOL_TOKENS keys
 *   - TIMEFRAMES <-> settings.CHART_TIMEFRAMES / broker_client.TIMEFRAME_TO_ANGEL_INTERVAL
 * Adding a symbol here without also adding it to SYMBOL_TOKENS (with its
 * Angel One symboltoken) will make the dropdown offer something the
 * backend 500s on -- add both sides together.
 */
// NIFTY/BANKNIFTY are the two this platform actually trades (backed by
// real HistoricalData via apps.market_data.tasks.ingest_watchlist_candles,
// same cadence apps.signals reads live). The rest are chart/dashboard-
// only -- real candle history too (apps.market_data.tasks.
// ingest_index_chart_candles, a separate lower-frequency job, see that
// task's own docstring for why it's deliberately not folded into the
// trading watchlist), just never evaluated for a trade signal. Symbol
// values match apps.investing.models.Index.symbol exactly (see
// seed_indices.py) so a chart pick and that index's ticker card always
// refer to the same underlying data.
export const SYMBOLS = [
  { value: "NIFTY", label: "NIFTY 50" },
  { value: "BANKNIFTY", label: "BANK NIFTY" },
  { value: "NIFTYIT", label: "NIFTY IT" },
  { value: "NIFTYAUTO", label: "NIFTY AUTO" },
  { value: "NIFTYFMCG", label: "NIFTY FMCG" },
  { value: "NIFTYPHARMA", label: "NIFTY PHARMA" },
  { value: "SENSEX", label: "SENSEX" },
];

// Matches Angel One's own chart interval picker.
export const TIMEFRAMES = [
  { value: "1m", label: "1 Minute" },
  { value: "3m", label: "3 Minutes" },
  { value: "5m", label: "5 Minutes" },
  { value: "10m", label: "10 Minutes" },
  { value: "15m", label: "15 Minutes" },
  { value: "30m", label: "30 Minutes" },
  { value: "1h", label: "1 Hour" },
  { value: "1d", label: "1 Day" },
];

export const DEFAULT_SYMBOL = "NIFTY";
export const DEFAULT_TIMEFRAME = "5m";
