import { useEffect, useMemo, useRef, useState } from "react";
import { Maximize2, Minimize2, RefreshCw } from "lucide-react";

import MACDPanel from "../charts/MACDPanel.jsx";
import PriceChart from "../charts/PriceChart.jsx";
import RSIPanel from "../charts/RSIPanel.jsx";
import DecisionFactors from "../components/DecisionFactors.jsx";
import EmptyState from "../components/EmptyState.jsx";
import ErrorState from "../components/ErrorState.jsx";
import LoadingSkeleton from "../components/LoadingSkeleton.jsx";
import OptionChainTable from "../components/OptionChainTable.jsx";
import OptionContractChartModal from "../components/OptionContractChartModal.jsx";
import SignalCard from "../components/SignalCard.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import TimeframeSelector from "../components/TimeframeSelector.jsx";
import Watchlist from "../components/Watchlist.jsx";
import { DEFAULT_TIMEFRAME, TIMEFRAMES } from "../constants/market.js";
import { useExpiryStatus } from "../hooks/useExpiryStatus.js";
import { useLiveIndexSpot } from "../hooks/useLiveIndexSpot.js";
import { useLiveOptionChainMerge } from "../hooks/useLiveOptionChainMerge.js";
import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";
import { useThemeStore } from "../store/themeStore.js";
import { isMarketOpenNow } from "../utils/marketHours.js";

// Only NIFTY/BANKNIFTY are actually evaluated for an options signal
// (apps.options.index_direction_strategy) -- the workspace's watchlist
// is deliberately scoped to those two, not the full chart-only SYMBOLS
// list constants/market.js exports for the Dashboard's chart picker.
const WORKSPACE_SYMBOLS = ["NIFTY", "BANKNIFTY"];
// Spec's recommended timeframe set for the trading workspace (1m/5m/
// 15m/1H/1D) -- a curated subset of the full TIMEFRAMES list, not a
// second definition of what a timeframe is.
const WORKSPACE_TIMEFRAME_VALUES = ["1m", "5m", "15m", "1h", "1d"];
const WORKSPACE_TIMEFRAMES = TIMEFRAMES.filter((t) => WORKSPACE_TIMEFRAME_VALUES.includes(t.value));
const TIMEFRAME_MINUTES = { "1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "1d": 1440 };

const selectStyle = {
  background: "var(--card)", color: "var(--text)",
  border: "1px solid var(--border)", borderRadius: 6, padding: 6,
};

/**
 * JARVIS AI Signal Terminal -- the professional trading-terminal view
 * the redesign asked for: underlying analysis -> option chain -> exact
 * resolved contract -> risk-validated JARVIS signal, all in one place,
 * instead of a bare "NIFTY -> BUY" line. Deliberately built by
 * REUSING the pieces that already exist rather than duplicating them.
 *
 * There is no manual "place order" endpoint anywhere in this backend
 * (apps.execution only exposes reading positions + the paper/live mode
 * toggle) -- trading is fully automated, signal -> risk engine -> auto
 * execution. The right-hand panel below is therefore an honest "Trade
 * Preview" of the latest real signal (SignalCard + DecisionFactors),
 * never a fake order form that would post nowhere.
 */
export default function JarvisSignalTerminal() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [signal, setSignal] = useState(null);
  const [signalHistory, setSignalHistory] = useState([]);
  const [evaluating, setEvaluating] = useState(false);
  const [evaluateError, setEvaluateError] = useState(null);

  // Single shared source of truth for expiry selection (apps.options.
  // expiry_service via /api/options/expiry-status/) -- see
  // hooks/useExpiryStatus.js's own docstring for why this replaced this
  // page's own "fetch expiries, auto-select first, never re-validate"
  // effect.
  const {
    expiry, setExpiry, availableExpiries: expiries, lastSuccessfulSync,
    rolloverRequired, unavailable: expiryUnavailable, error: expiryError,
  } = useExpiryStatus(underlying);
  const [chainRows, setChainRows] = useState([]);
  // restSpot is the one-time snapshot from the /options/chain/ REST
  // response; `spot` prefers a live index-price tick (useLiveIndexSpot
  // -- same /ws/investing/index-live/ push the Dashboard's index
  // tickers already use) once one arrives, falling back to restSpot
  // until then, so the displayed spot never silently goes stale the
  // way it used to (option premiums in the chain already updated live
  // via latestOptionUpdate; the underlying's own price didn't).
  const [restSpot, setRestSpot] = useState(null);
  const liveSpot = useLiveIndexSpot(underlying);
  const spot = liveSpot ?? restSpot;
  const chainContainerRef = useRef(null);
  const atmRowRef = useRef(null);
  const [selectedContract, setSelectedContract] = useState(null);
  const [flash, setFlash] = useState({});

  const [positions, setPositions] = useState([]);
  const [executionMode, setExecutionModeState] = useState(null);
  const [strategySettings, setStrategySettings] = useState(null);

  const [timeframe, setTimeframe] = useState(DEFAULT_TIMEFRAME);
  const [candles, setCandles] = useState([]);
  const [candlesLoading, setCandlesLoading] = useState(true);
  const [candlesError, setCandlesError] = useState(null);
  const [candlesRetryToken, setCandlesRetryToken] = useState(0);
  const [indicators, setIndicators] = useState([]);
  const [activeTab, setActiveTab] = useState("positions");
  const [fullscreen, setFullscreen] = useState(false);
  const chartHostRef = useRef(null);
  const lastTickAtRef = useRef(null);
  const [tickTock, setTickTock] = useState(0); // forces the staleness badge to re-evaluate on an interval

  const latestSignal = useLiveStore((s) => s.latestSignal);
  const latestOptionUpdate = useLiveStore((s) => s.latestOptionUpdate);
  const latestCandle = useLiveStore((s) => s.latestCandle);
  const riskAlerts = useLiveStore((s) => s.riskAlerts);
  const theme = useThemeStore((s) => s.theme);

  const fetchLatestSignal = () => {
    endpoints.signals({ symbol: underlying, page_size: 1 }).then((res) => {
      setSignal(res.data.results?.[0] ?? null);
    });
  };

  useEffect(() => {
    fetchLatestSignal();
    endpoints.signals({ symbol: underlying, page_size: 20 }).then((res) => {
      setSignalHistory(res.data.results ?? []);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [underlying]);

  const lastSeenSignalIdRef = useRef(null);
  useEffect(() => {
    if (!latestSignal || latestSignal.symbol !== underlying) return;
    if (latestSignal.id === lastSeenSignalIdRef.current) return;
    lastSeenSignalIdRef.current = latestSignal.id;
    fetchLatestSignal();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [latestSignal, underlying]);

  useEffect(() => {
    endpoints.positions().then((res) => setPositions(res.data.results ?? []));
    endpoints.executionMode().then((res) => setExecutionModeState(res.data));
    endpoints.optionsStrategySettings().then((res) => setStrategySettings(res.data));
  }, []);

  // Chain rows reset whenever the underlying changes -- the expiry
  // itself is now owned by useExpiryStatus above (it re-defaults on
  // underlying change too, see that hook's own effect).
  useEffect(() => {
    setChainRows([]);
  }, [underlying]);

  useEffect(() => {
    if (!expiry) return;
    let cancelled = false;
    endpoints.optionChain(underlying, expiry).then((res) => {
      if (cancelled) return;
      setChainRows(res.data.rows ?? []);
      setRestSpot(res.data.spot ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [underlying, expiry]);

  // See hooks/useLiveOptionChainMerge.js for why this must be a
  // field-level merge, not a whole-leg replace.
  useLiveOptionChainMerge({ latestOptionUpdate, underlying, expiry, setChainRows, setFlash });

  // Candlestick chart -- same load pattern Dashboard.jsx uses, including
  // its own candlesError/candlesRetryToken (a real fetch failure/timeout
  // must not collapse into the same "no candles yet" empty state a
  // genuinely cold-started symbol shows -- see Dashboard.jsx's own
  // comment on why, and services/api.js's new REQUEST_TIMEOUT_MS).
  useEffect(() => {
    let cancelled = false;
    setCandlesLoading(true);
    setCandlesError(null);
    endpoints.candles(underlying, timeframe).then((res) => {
      if (!cancelled) setCandles(res.data.results ?? []);
    }).catch((err) => {
      if (cancelled) return;
      setCandles([]);
      setCandlesError(
        err.code === "ECONNABORTED"
          ? "The request timed out. The backend may be under load -- try again."
          : "Could not load candle data."
      );
    }).finally(() => {
      if (!cancelled) setCandlesLoading(false);
    });
    endpoints.indicators(underlying, timeframe).then((res) => {
      if (!cancelled) setIndicators(res.data.results ?? []);
    }).catch(() => {
      if (!cancelled) setIndicators([]);
    });
    return () => {
      cancelled = true;
    };
  }, [underlying, timeframe, candlesRetryToken]);

  const liveCandle =
    !candlesLoading && latestCandle && latestCandle.symbol === underlying && latestCandle.timeframe === timeframe
      ? latestCandle
      : null;

  useEffect(() => {
    if (liveCandle) lastTickAtRef.current = Date.now();
  }, [liveCandle]);

  // Data-status badge for the chart: real, timestamp-based staleness
  // (no live tick for 3x the selected timeframe's own interval, floored
  // at 2 minutes) rather than a fixed guess -- gated on real market
  // hours so it reads "Market Closed" instead of a false "Stale"
  // outside the 09:15-15:30 IST session, when no ticks are expected at
  // all.
  useEffect(() => {
    const id = setInterval(() => setTickTock((t) => t + 1), 15000);
    return () => clearInterval(id);
  }, []);
  const dataStatus = useMemo(() => {
    void tickTock;
    if (!isMarketOpenNow()) return { tone: "muted", label: "Market Closed" };
    if (!lastTickAtRef.current) return { tone: "warn", label: "Awaiting live tick" };
    const thresholdMs = Math.max(2, (TIMEFRAME_MINUTES[timeframe] ?? 5) * 3) * 60000;
    const stale = Date.now() - lastTickAtRef.current > thresholdMs;
    return stale ? { tone: "warn", label: "Stale" } : { tone: "ok", label: "Live" };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tickTock, timeframe]);

  const toggleFullscreen = () => {
    if (!document.fullscreenElement) {
      chartHostRef.current?.requestFullscreen?.();
    } else {
      document.exitFullscreen?.();
    }
  };
  useEffect(() => {
    const onChange = () => setFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const atmStrike = useMemo(() => {
    if (spot == null || chainRows.length === 0) return null;
    return chainRows.reduce(
      (closest, row) => (Math.abs(row.strike - spot) < Math.abs(closest - spot) ? row.strike : closest),
      chainRows[0].strike
    );
  }, [chainRows, spot]);

  const signalStrike = signal?.strike_price != null ? Number(signal.strike_price) : null;
  const matchingChainRow = useMemo(
    () => (signalStrike != null ? chainRows.find((r) => r.strike === signalStrike) : null),
    [chainRows, signalStrike]
  );

  const underlyingPositions = positions.filter((p) => p.symbol?.startsWith(underlying));

  const handleEvaluateNow = () => {
    setEvaluating(true);
    setEvaluateError(null);
    endpoints.evaluateNow(underlying).then((res) => {
      setSignal(res.data);
      setEvaluating(false);
    }).catch((err) => {
      setEvaluateError(err.response?.data?.error || "Re-analysis failed.");
      setEvaluating(false);
    });
  };

  const handleStrategySettingChange = (field, value) => {
    const next = { ...strategySettings, [field]: value };
    setStrategySettings(next);
    endpoints.setOptionsStrategySettings({ [field]: value }).then((res) => setStrategySettings(res.data));
  };

  const TABS = [
    { key: "positions", label: `Positions (${underlyingPositions.length})` },
    { key: "signals", label: `Signal History (${signalHistory.length})` },
    { key: "risk", label: `Risk Alerts (${riskAlerts.length})` },
  ];

  return (
    <div>
      <div className="workspace-grid">
        <div className="workspace-col">
          <div className="panel" style={{ height: "100%" }}>
            <h3>Watchlist</h3>
            <Watchlist symbols={WORKSPACE_SYMBOLS} selected={underlying} onSelect={setUnderlying} />
          </div>
        </div>

        <div className="workspace-col">
          <div className="panel chart-fullscreen-host" ref={chartHostRef}>
            <div className="chart-toolbar">
              <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
                <h3 style={{ margin: 0 }}>
                  {underlying}
                  {spot != null && (
                    <span style={{ fontSize: 13, fontWeight: 400, color: "var(--muted)", marginLeft: 8 }}>
                      Spot {spot.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                      {atmStrike != null && ` · ATM ${atmStrike}`}
                    </span>
                  )}
                </h3>
                <StatusBadge tone={dataStatus.tone}>{dataStatus.label}</StatusBadge>
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <TimeframeSelector options={WORKSPACE_TIMEFRAMES} value={timeframe} onChange={setTimeframe} />
                <button
                  type="button"
                  className="icon-btn"
                  onClick={toggleFullscreen}
                  title={fullscreen ? "Exit fullscreen" : "Fullscreen chart"}
                  aria-label={fullscreen ? "Exit fullscreen" : "Fullscreen chart"}
                >
                  {fullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
                </button>
              </div>
            </div>

            {candlesLoading ? (
              <LoadingSkeleton height={360} />
            ) : candlesError ? (
              <ErrorState
                title="Couldn't load the chart"
                detail={candlesError}
                onRetry={() => setCandlesRetryToken((t) => t + 1)}
              />
            ) : candles.length === 0 ? (
              <EmptyState title={`No ${underlying} ${timeframe} candles yet`} detail="Run the ingestion task or backfill command to populate candle history." />
            ) : (
              <>
                <PriceChart candles={candles} liveCandle={liveCandle} indicators={indicators} theme={theme} />
                {indicators.length > 0 && (
                  <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
                    <RSIPanel indicators={indicators} theme={theme} />
                    <MACDPanel indicators={indicators} theme={theme} />
                  </div>
                )}
              </>
            )}
          </div>

          <div className="panel">
            <h3 style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span>Option Chain — {underlying} {expiry && `· ${expiry}`}</span>
              {rolloverRequired && !expiryUnavailable && (
                <StatusBadge tone="warn">Rollover pending</StatusBadge>
              )}
              {lastSuccessfulSync && (
                <span style={{ fontSize: 11, fontWeight: 400, color: "var(--muted)", textTransform: "none", letterSpacing: 0 }}>
                  Contracts synced {new Date(lastSuccessfulSync).toLocaleString()}
                </span>
              )}
            </h3>
            {expiryError ? (
              <EmptyState title="Could not load expiry information" detail={expiryError} />
            ) : expiryUnavailable ? (
              <EmptyState
                title="Contract sync temporarily unavailable"
                detail={`No valid, non-expired expiry is available for ${underlying} right now -- a rollover resync is required and should complete automatically shortly.`}
              />
            ) : expiries.length === 0 ? (
              <EmptyState title={`No expiries synced yet for ${underlying}`} />
            ) : (
              <OptionChainTable
                rows={chainRows}
                spot={spot}
                atmStrike={atmStrike}
                flash={flash}
                aiSelectedStrike={signalStrike}
                aiSelectedSide={signal?.option_side}
                selectedContractStrike={signal?.status === "approved" || signal?.status === "executed" ? signalStrike : null}
                selectedContractSide={signal?.status === "approved" || signal?.status === "executed" ? signal?.option_side : null}
                containerRef={chainContainerRef}
                atmRowRef={atmRowRef}
                onCellClick={(strike, optionType) => setSelectedContract({ strike, optionType })}
              />
            )}
          </div>
        </div>

        <div className="workspace-col">
          <div className="panel">
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
              <h3 style={{ margin: 0 }}>Trade Preview</h3>
              {executionMode && (
                <span className={`badge ${executionMode.mode === "live" ? "badge-red" : "badge-green"}`}>
                  {executionMode.mode === "live" ? "LIVE" : "PAPER"}
                </span>
              )}
            </div>
            <button
              type="button"
              className="btn btn-sm"
              onClick={handleEvaluateNow}
              disabled={evaluating}
              style={{ width: "100%", marginBottom: 8, borderColor: "var(--accent)", color: "var(--accent)" }}
            >
              <RefreshCw size={13} className={evaluating ? "spin" : undefined} />
              {evaluating ? "Analyzing…" : "Re-analyze Now"}
            </button>
            {evaluateError && <p style={{ color: "var(--red)", fontSize: 12, margin: "0 0 8px" }}>{evaluateError}</p>}

            {strategySettings && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 10, fontSize: 12, color: "var(--muted)" }}>
                <label>
                  Expiry mode:{" "}
                  <select
                    value={strategySettings.expiry_mode}
                    onChange={(e) => handleStrategySettingChange("expiry_mode", e.target.value)}
                    style={{ ...selectStyle, padding: 3, fontSize: 12 }}
                  >
                    <option value="current_week">Current Week</option>
                    <option value="next_week">Next Week</option>
                    <option value="monthly">Monthly</option>
                    <option value="custom">Custom</option>
                  </select>
                </label>
                <label>
                  Strike mode:{" "}
                  <select
                    value={strategySettings.strike_mode}
                    onChange={(e) => handleStrategySettingChange("strike_mode", e.target.value)}
                    style={{ ...selectStyle, padding: 3, fontSize: 12 }}
                  >
                    <option value="atm">ATM</option>
                    <option value="itm">ITM</option>
                    <option value="otm">OTM</option>
                    <option value="ai">AI Selected</option>
                  </select>
                </label>
              </div>
            )}

            <p style={{ fontSize: 11.5, color: "var(--muted)", margin: "0 0 10px", lineHeight: 1.5 }}>
              This platform trades automatically from approved signals under the current Paper/Live mode
              (manage it in Settings) — there is no manual order entry in Phase 1.
            </p>

            <SignalCard signal={signal} />
          </div>
          <div className="panel">
            <DecisionFactors signal={signal} chainRow={matchingChainRow} />
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="tabs-bar">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              className={`tab-btn${activeTab === t.key ? " active" : ""}`}
              onClick={() => setActiveTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {activeTab === "positions" && (
          underlyingPositions.length === 0 ? (
            <EmptyState title="No open positions" detail={`No open ${underlying} positions right now.`} />
          ) : (
            <div className="data-table-wrap">
              <table className="data-table">
                <thead>
                  <tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Unrealized P&amp;L</th></tr>
                </thead>
                <tbody>
                  {underlyingPositions.map((p) => (
                    <tr key={p.id}>
                      <td>{p.symbol}</td>
                      <td>{p.side.toUpperCase()}</td>
                      <td className="num">{p.qty}</td>
                      <td className="num">{p.entry_price}</td>
                      <td className={Number(p.unrealized_pnl) >= 0 ? "num-positive" : "num-negative"}>{p.unrealized_pnl}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}

        {activeTab === "signals" && (
          signalHistory.length === 0 ? (
            <EmptyState title="No signals yet" />
          ) : (
            <div className="data-table-wrap">
              <table className="data-table">
                <thead>
                  <tr><th>Status</th><th>Contract</th><th>Score</th><th>When</th></tr>
                </thead>
                <tbody>
                  {signalHistory.map((s) => (
                    <tr key={s.id} onClick={() => setSignal(s)} style={{ cursor: "pointer" }}>
                      <td>
                        <StatusBadge tone={s.status === "approved" || s.status === "executed" ? "ok" : s.status === "rejected" ? "bad" : "muted"}>
                          {s.status}
                        </StatusBadge>
                      </td>
                      <td>{s.strike_price != null ? `${Number(s.strike_price)} ${s.option_side}` : "—"}</td>
                      <td className="num">{s.total_score?.toFixed?.(2) ?? "—"}</td>
                      <td style={{ color: "var(--muted)" }}>{new Date(s.created_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}

        {activeTab === "risk" && (
          riskAlerts.length === 0 ? (
            <EmptyState title="No risk alerts" detail="Live risk events pushed since this page loaded will appear here." />
          ) : (
            <div className="data-table-wrap">
              <table className="data-table">
                <thead>
                  <tr><th>Severity</th><th>Event</th><th>Message</th></tr>
                </thead>
                <tbody>
                  {riskAlerts.map((a, i) => (
                    <tr key={i}>
                      <td><StatusBadge tone={a.severity === "critical" ? "bad" : a.severity === "warning" ? "warn" : "muted"}>{a.severity}</StatusBadge></td>
                      <td>{a.event_type}</td>
                      <td>{a.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}
      </div>

      <OptionContractChartModal
        contract={selectedContract}
        underlying={underlying}
        expiry={expiry}
        chainRows={chainRows}
        onClose={() => setSelectedContract(null)}
      />
    </div>
  );
}
