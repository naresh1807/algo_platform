import axios from "axios";

import { clearAuthenticationToken } from "../utils/websocket.js";

/**
 * Single axios instance for all REST calls. The Vite dev-server proxy
 * (vite.config.js) forwards "/api/*" to Django, so this stays relative
 * -- no environment-specific base URL branching needed in dev, and in
 * production the same relative path works as long as Django is served
 * from the same origin (or behind the same reverse proxy).
 */
// No timeout was set at all before this (axios defaults to 0 = never
// times out) -- meaning a single stalled request (a DB lock held by
// concurrent candle ingestion, a dropped connection, a briefly
// unresponsive dev server) could leave a loading spinner spinning
// forever with zero feedback and no way to recover short of a full
// page reload. 20s is generous for even the heaviest real request this
// app makes (a 2000-row candle fetch normally completes in well under
// 1s) while still bounding the worst case to something a user would
// actually wait through before seeing a real error instead of an
// indefinite spinner.
const REQUEST_TIMEOUT_MS = 20000;

export const api = axios.create({
  baseURL: "/api",
  timeout: REQUEST_TIMEOUT_MS,
});

// Attach the DRF token from localStorage, if present. The manual's
// "frontend should only display backend-approved decisions, not compute
// trading logic locally" principle means every one of these calls is
// read-heavy (signals, positions, metrics) -- there is deliberately no
// client-side trading logic to keep secrets for beyond this token.
api.interceptors.request.use((config) => {
  const token = localStorage.getItem("authToken");
  if (token) {
    config.headers.Authorization = `Token ${token}`;
  }
  return config;
});

// Only 401 means the credential itself is missing, expired, or revoked.
// A 403 is an authenticated RBAC denial and must be shown to the caller;
// treating it as logout would destroy a valid session and obscure the
// real authorization problem.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      clearAuthenticationToken();
      if (window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    }
    return Promise.reject(error);
  }
);

/**
 * Revoke the current server-side token before removing the local copy.
 * Logout remains reliable when Django is temporarily unreachable: the
 * request is best-effort, while local sockets/session state are always
 * cleared after the bounded attempt finishes.
 */
export async function logoutSession() {
  try {
    await api.post("/auth/logout/", null, { timeout: 5000 });
  } catch {
    // The token may already be expired/revoked or the backend may be
    // unavailable. Local logout must still complete in every case.
  } finally {
    clearAuthenticationToken();
  }
}

export const endpoints = {
  // page_size=2000 -- without this, DRF's project-wide PAGE_SIZE=100
  // default would silently truncate any symbol with more candles
  // ingested than that to just its most recent 100 (see
  // market_data.views.CandlePagination). 2000 covers a full trading
  // day even on a 1m timeframe with headroom.
  candles: (symbol, timeframe) =>
    api.get("/market-data/candles/", { params: { symbol, timeframe, page_size: 2000 } }),
  indicators: (symbol, timeframe) =>
    api.get("/market-data/indicators/", { params: { symbol, timeframe } }),
  // page_size defaults below (500) mirror the exact fix already applied
  // to `candles` above -- these four endpoints previously had NO
  // page_size override at all, so any symbol/day that accumulated more
  // than DRF's project-wide PAGE_SIZE=100 default silently lost rows
  // off these pages with no pagination UI to notice or work around it.
  // Callers can still pass their own page_size via `params` (it's
  // spread after the default, so it wins).
  signals: (params) => api.get("/signals/", { params: { page_size: 500, ...params } }),
  positions: () => api.get("/execution/positions/", { params: { page_size: 500 } }),
  // apps.execution.views.ExecutionModeView -- the Settings page's
  // Paper/Live trading toggle. confirmPhrase is only checked
  // server-side when mode="live" (switching back to paper never
  // requires it) -- see that view's own docstring for why.
  executionMode: () => api.get("/execution/mode/"),
  setExecutionMode: (mode, confirmPhrase) =>
    api.post("/execution/mode/", { mode, confirm_phrase: confirmPhrase }),
  riskEvents: (params) => api.get("/risk/events/", { params: { page_size: 500, ...params } }),
  killSwitch: () => api.get("/risk/kill-switch/"),
  equity: () => api.get("/risk/equity/"),
  // apps.risk.views.EquityCurveView -- the raw EquitySnapshot time series
  // for the performance dashboard's equity curve chart.
  equityCurve: (lookback_days = 30) => api.get("/risk/equity/curve/", { params: { lookback_days } }),
  performance: () => api.get("/analytics/performance/", { params: { page_size: 500 } }),
  // apps.analytics.views.DailyPnLReportView -- today's row is always
  // refreshed live server-side (today isn't over yet), past days read
  // straight from the stored PerformanceMetrics table.
  dailyPnLReport: (days = 30) => api.get("/analytics/daily-pnl/", { params: { days } }),
  // apps.analytics.views.SharpeRatioView / PerformanceBreakdownView --
  // Phase G of the Options Intelligence Engine's performance dashboard.
  sharpeRatio: (lookback_days = 30) => api.get("/analytics/sharpe-ratio/", { params: { lookback_days } }),
  performanceBreakdown: (lookback_days = 30) =>
    api.get("/analytics/performance-breakdown/", { params: { lookback_days } }),
  dailyReviews: () => api.get("/learning/daily-reviews/", { params: { page_size: 500 } }),
  driftEvents: () => api.get("/learning/drift-events/", { params: { page_size: 500 } }),
  strategyVersions: () => api.get("/learning/strategy-versions/", { params: { page_size: 500 } }),
  modelRegistry: () => api.get("/learning/model-registry/", { params: { page_size: 500 } }),
  tradeReviews: () => api.get("/learning/trade-reviews/", { params: { page_size: 500 } }),
  // apps.learning.strategy_methods' isolated paper-comparison trades --
  // method is optional (omit for both swing + scalping groups; the
  // Scalping page passes the three scalping method names).
  hypotheticalTrades: (params) => api.get("/learning/hypothetical-trades/", { params: { page_size: 100, ...params } }),
  // symbol/sentiment_label both optional -- omitting symbol returns
  // every headline (general market feeds + both watchlist symbols'
  // targeted searches, see apps.news.rss_client), not just one.
  newsSentiment: (symbol, sentiment_label, page_size = 50) =>
    api.get("/news/sentiment/", { params: { symbol, sentiment_label, page_size } }),
  feedHealth: () => api.get("/monitoring/feed-health/"),
  // apps.monitoring.health.SystemHealthView -- Django/MySQL/Redis/Celery
  // worker+beat heartbeats, live Angel One feed connection state and
  // tick staleness, subscribed option-token count, selected expiry per
  // underlying. See hooks/useSystemHealth.js for how this is turned
  // into a single status badge.
  systemHealth: () => api.get("/monitoring/health/"),
  priceAlerts: () => api.get("/monitoring/price-alerts/"),
  createPriceAlert: (symbol, condition, target_price) =>
    api.post("/monitoring/price-alerts/", { symbol, condition, target_price }),
  deletePriceAlert: (id) => api.delete(`/monitoring/price-alerts/${id}/`),

  // apps.investing -- long-term stock investing (trader-requested, not
  // manual-specified). Separate from the intraday options/signals APIs.
  stockWatchlist: () => api.get("/investing/watchlist/"),
  addStockToWatchlist: (stock_symbol, notes = "") =>
    api.post("/investing/watchlist/", { stock_symbol, notes }),
  removeStockFromWatchlist: (id) => api.delete(`/investing/watchlist/${id}/`),
  stockRecommendations: () => api.get("/investing/recommendations/"),
  stockFundamentals: (stockId) => api.get("/investing/fundamentals/", { params: { stock: stockId } }),
  ipoListings: (status) => api.get("/investing/ipos/", status ? { params: { status } } : {}),
  indices: () => api.get("/investing/indices/"),
  indexConstituents: (indexId) => api.get("/investing/index-constituents/", { params: { index: indexId } }),
  sectorBreakdown: () => api.get("/investing/sector-breakdown/"),
  marketOutlook: () => api.get("/jarvis/market-outlook/"),
  marketOutlookHistory: () => api.get("/jarvis/market-outlook/history/"),
  optionsAnalytics: (underlying, expiry) =>
    api.get("/options/analytics/", { params: { underlying, expiry } }),
  optionExpiries: (underlying) => api.get("/options/expiries/", { params: { underlying } }),
  // apps.options.views.OptionExpiryStatusView -- current_expiry/
  // next_expiry/available_expiries/last_successful_sync/rollover_required
  // in one call, backend-computed via apps.options.expiry_service (the
  // single source of truth for expiry rollover -- the frontend never
  // guesses an expiry weekday itself). 503 when no valid expiry exists
  // and sync can't recover; axios resolves 503 as a normal response
  // here (not a thrown error) since callers need the body (rollover_required)
  // to render a real message, not just a caught exception.
  optionExpiryStatus: (underlying) =>
    api.get("/options/expiry-status/", { params: { underlying }, validateStatus: (s) => s === 200 || s === 503 }),
  // apps.options.views.SelectedExpiryView -- publishes the operator's
  // expiry choice so apps.market_data.broker_ws_client's dynamic
  // subscription-refresh loop (a separate OS process, see that view's
  // own docstring) picks it up on its next cycle and re-subscribes the
  // live feed to that expiry's strikes, without restarting anything.
  setSelectedOptionExpiry: (underlying, expiry) =>
    api.post("/options/selected-expiry/", { underlying, expiry }),
  optionChain: (underlying, expiry) => api.get("/options/chain/", { params: { underlying, expiry } }),
  // apps.options.views.OptionCandlesView -- one exact option contract's
  // own OHLCV candles (never the underlying's). `params` accepts either
  // {contract} or {token} (once already known, e.g. for a WS resub) or
  // {underlying, expiry, strike, option_type} (exactly what an option-
  // chain row click has before any token is known) plus {timeframe} and
  // optional {from, to} -- the backend resolves/validates the exact
  // contract server-side, this app never constructs a token itself.
  // Undefined keys are dropped by axios's own param serializer, so
  // callers can pass every field and let whichever identity applies win.
  optionCandles: (params) => api.get("/options/candles/", { params }),
  bestStrike: (underlying, expiry, direction) =>
    api.get("/options/best-strike/", { params: { underlying, expiry, direction } }),
  // apps.options.views.OptionsStrategySettingView -- the expiry/strike
  // mode preferences (current week/next week/monthly/custom expiry;
  // ATM/ITM/OTM/AI strike selection) apps.options.index_direction_strategy
  // reads on every scheduled evaluation. Same GET/POST singleton pattern
  // as executionMode()/setExecutionMode() above.
  optionsStrategySettings: () => api.get("/options/strategy-settings/"),
  setOptionsStrategySettings: (settings) => api.post("/options/strategy-settings/", settings),
  // apps.options.views.EvaluateNowView -- manual "Re-analyze Now" trigger
  // for apps.options.index_direction_strategy.evaluate_index_direction_trade,
  // instead of waiting up to 5 minutes for the next scheduled beat tick.
  // Server-side throttled per underlying (EVALUATE_NOW_COOLDOWN_SECONDS).
  evaluateNow: (underlying, timeframe = "5m") =>
    api.post("/options/evaluate/", { underlying, timeframe }),
  // One contract's own LTP/OI/volume/IV history (OptionChainSnapshotViewSet
  // filtered down to a single strike+side) -- powers the option-chain
  // "click a strike to see its own price chart" popup, page_size=200 to
  // cover a full trading day's worth of the ~5-minute ingestion cadence.
  optionContractHistory: (underlying, expiry, strike, optionType) =>
    api.get("/options/snapshots/", {
      params: {
        contract__underlying: underlying, contract__expiry: expiry,
        contract__strike: strike, contract__option_type: optionType,
        page_size: 200,
      },
    }),
  marketSummary: (date) => api.get("/news/market-summary/", { params: { date } }),
  jarvisCommand: (text, input_mode = "text", confirm = false) =>
    api.post("/jarvis/command/", { text, input_mode, confirm }),
  jarvisHistory: () => api.get("/jarvis/history/"),
  deleteJarvisHistory: () => api.delete("/jarvis/history/"),
  jarvisSuggested: (category) => api.get("/jarvis/suggested/", { params: category ? { category } : {} }),
};
