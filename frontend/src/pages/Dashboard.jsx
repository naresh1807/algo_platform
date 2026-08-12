import { useEffect, useState } from "react";

import MACDPanel from "../charts/MACDPanel.jsx";
import PriceChart from "../charts/PriceChart.jsx";
import RSIPanel from "../charts/RSIPanel.jsx";
import PriceAlertsPanel from "../components/PriceAlertsPanel.jsx";
import IndexTickerBar from "../components/IndexTickerBar.jsx";
import MarketOutlookPanel from "../components/MarketOutlookPanel.jsx";
import { DEFAULT_SYMBOL, DEFAULT_TIMEFRAME, SYMBOLS, TIMEFRAMES } from "../constants/market.js";
import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";
import { useThemeStore } from "../store/themeStore.js";

/**
 * This page's panel list is a direct 1:1 mapping of manual section 18
 * ("Dashboard Requirements") -- chart, indicators, signal score,
 * sentiment score, regime status, risk state, open positions, P&L,
 * drawdown, daily review notes, drift alerts, strategy version,
 * kill-switch state. Rejected-trade reasons (section 8) live in the
 * Signals page rather than duplicated here, since that list can get
 * long -- this page only shows the most recent one as a teaser.
 *
 * Every panel reads real values from state that starts `null`/`[]` and
 * a loading flag, rather than showing fabricated placeholder numbers --
 * same "don't fake it" principle used throughout BharatHub's home page
 * (see the developer manual precedent: real stats or nothing, no fallback
 * to made-up numbers).
 */
export default function Dashboard() {
  // The chart's own symbol/timeframe selection -- independent of the
  // rest of the panels below (signals, positions, etc. still cover
  // the whole watchlist). Changing either re-fetches candles AND
  // re-points the live-candle filter (see the [symbol, timeframe]
  // effect further down) at the newly selected series.
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [timeframe, setTimeframe] = useState(DEFAULT_TIMEFRAME);
  const [candles, setCandles] = useState([]);
  const [candlesLoading, setCandlesLoading] = useState(true);
  const [indicators, setIndicators] = useState([]);
  const [signals, setSignals] = useState([]);
  const [positions, setPositions] = useState([]);
  const [performance, setPerformance] = useState(null);
  const [dailyReview, setDailyReview] = useState(null);
  const [driftEvents, setDriftEvents] = useState([]);
  const [strategyVersion, setStrategyVersion] = useState(null);
  const [riskEvents, setRiskEvents] = useState([]);
  const [marketSummary, setMarketSummary] = useState(null);
  const [loadError, setLoadError] = useState(null);

  const latestSignal = useLiveStore((s) => s.latestSignal);
  const latestCandle = useLiveStore((s) => s.latestCandle);
  const killSwitchActive = useLiveStore((s) => s.killSwitchActive);
  const theme = useThemeStore((s) => s.theme);

  useEffect(() => {
    // Live sockets are connected once at the AppShell level (App.jsx),
    // not per-page -- see liveStore.js's module docstring for why.

    Promise.allSettled([
      endpoints.signals({}),
      endpoints.positions(),
      endpoints.performance(),
      endpoints.dailyReviews(),
      endpoints.driftEvents(),
      endpoints.strategyVersions(),
      endpoints.riskEvents({ severity: "critical" }),
      endpoints.marketSummary(),
    ]).then(
      ([
        signalsRes,
        positionsRes,
        perfRes,
        reviewRes,
        driftRes,
        strategyRes,
        riskRes,
        summaryRes,
      ]) => {
        if (signalsRes.status === "fulfilled") setSignals(signalsRes.value.data.results ?? []);
        if (positionsRes.status === "fulfilled") setPositions(positionsRes.value.data.results ?? []);
        if (perfRes.status === "fulfilled") setPerformance(perfRes.value.data.results?.[0] ?? null);
        if (reviewRes.status === "fulfilled") setDailyReview(reviewRes.value.data.results?.[0] ?? null);
        if (driftRes.status === "fulfilled") setDriftEvents(driftRes.value.data.results ?? []);
        if (strategyRes.status === "fulfilled") {
          const active = (strategyRes.value.data.results ?? []).find((v) => v.active_flag);
          setStrategyVersion(active ?? null);
        }
        if (riskRes.status === "fulfilled") setRiskEvents(riskRes.value.data.results ?? []);
        if (summaryRes.status === "fulfilled") setMarketSummary(summaryRes.value.data.summary);

        // If literally every call failed, the backend probably isn't
        // running yet -- surface one clear message instead of seven
        // separate silent failures.
        const allFailed = [
          signalsRes, positionsRes, perfRes, reviewRes, driftRes, strategyRes, riskRes,
        ].every((r) => r.status === "rejected");
        if (allFailed) {
          setLoadError("Could not reach the backend API. Is Django running on 127.0.0.1:8000?");
        }
      }
    );
  }, []);

  // Re-fetches whenever the symbol or timeframe dropdown changes --
  // this is the only network call the selectors trigger; the ingestion
  // side (which timeframes actually get pulled from Angel One) is
  // controlled independently by settings.CHART_TIMEFRAMES, not by
  // what's currently selected here.
  useEffect(() => {
    let cancelled = false;
    setCandlesLoading(true);
    endpoints
      .candles(symbol, timeframe)
      .then((res) => {
        if (!cancelled) setCandles(res.data.results ?? []);
      })
      .catch(() => {
        if (!cancelled) setCandles([]);
      })
      .finally(() => {
        if (!cancelled) setCandlesLoading(false);
      });

    // Indicators load independently of candles: a symbol can have
    // enough candles to draw a chart but fewer than
    // indicators.MIN_CANDLES_REQUIRED, in which case the endpoint
    // returns an empty series and the chart should still show candles
    // with no overlay/panels rather than failing.
    endpoints
      .indicators(symbol, timeframe)
      .then((res) => {
        if (!cancelled) setIndicators(res.data.results ?? []);
      })
      .catch(() => {
        if (!cancelled) setIndicators([]);
      });

    return () => {
      cancelled = true;
    };
  }, [symbol, timeframe]);

  // Live candle pushes (apps/market_data/signals.py's WebSocket
  // broadcast, now arriving tick-by-tick from
  // apps/market_data/tick_aggregator.py rather than once a minute) are
  // handed straight to PriceChart as `liveCandle` -- it applies them
  // incrementally via lightweight-charts' own update() API rather than
  // this component maintaining a merged copy in React state. Only
  // matters for the symbol/timeframe actually being charted here (the
  // WebSocket group is shared across the whole watchlist, see
  // apps/market_data/consumers.py's docstring for why); also withheld
  // while `candles` is still (re)loading after a symbol/timeframe
  // switch, so a live message for the newly selected series can't be
  // applied to the chart before its own history has finished loading.
  const liveCandle =
    !candlesLoading && latestCandle && latestCandle.symbol === symbol && latestCandle.timeframe === timeframe
      ? latestCandle
      : null;

  const latestRejected = signals.find((s) => s.status === "rejected");

  // Signal Status/Sentiment Score/Market Regime below previously read
  // ONLY `latestSignal` (the live WebSocket push) -- correct once a
  // signal has fired since this tab opened, but blank on every fresh
  // page load until the next one arrives (signals fire every ~5 min),
  // even though `signals` (fetched via REST above, ordered -created_at
  // per the backend) already has the real most recent one. Falls back
  // to that REST-fetched value; a live push still overrides it the
  // instant one arrives, same as before.
  const displaySignal = latestSignal ?? signals[0] ?? null;

  return (
    <div>
      {loadError && (
        <div className="panel" style={{ borderColor: "var(--red)" }}>
          ⚠️ {loadError}
        </div>
      )}

      <IndexTickerBar />

      <MarketOutlookPanel />

      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 style={{ margin: 0 }}>
            Live Chart — {symbol} ({TIMEFRAMES.find((t) => t.value === timeframe)?.label ?? timeframe})
          </h3>
          <div style={{ display: "flex", gap: 8 }}>
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              {SYMBOLS.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
            <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
              {TIMEFRAMES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>
        </div>
        {candlesLoading ? (
          <p style={{ color: "var(--muted)" }}>Loading {symbol} candles…</p>
        ) : candles.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>
            No {symbol} {timeframe} candles yet -- run the ingestion task (BROKER_MODE=live) or the backfill command.
          </p>
        ) : (
          <>
            <PriceChart candles={candles} liveCandle={liveCandle} indicators={indicators} theme={theme} />
            {indicators.length === 0 ? (
              <p style={{ color: "var(--muted)", fontSize: 12, marginTop: 8 }}>
                Not enough history yet for indicator overlays/panels (needs 60+ candles).
              </p>
            ) : (
              <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
                <div>
                  <p style={{ margin: "0 0 4px", fontSize: 12, color: "var(--muted)" }}>RSI (14)</p>
                  <RSIPanel indicators={indicators} theme={theme} />
                </div>
                <div>
                  <p style={{ margin: "0 0 4px", fontSize: 12, color: "var(--muted)" }}>MACD (12, 26, 9)</p>
                  <MACDPanel indicators={indicators} theme={theme} />
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <div className="panel">
        <h3>AI Market News Summary</h3>
        <p style={{ color: marketSummary ? "var(--text)" : "var(--muted)" }}>
          {marketSummary ?? "No market summary available yet -- needs today's news polling to have run."}
        </p>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
        <div className="panel">
          <h3>Signal Status</h3>
          {displaySignal ? (
            <p style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span style={{ fontWeight: 600 }}>{displaySignal.symbol}</span>
              <span className={`badge ${displaySignal.signal_type === "buy" ? "badge-green" : "badge-muted"}`}>
                {displaySignal.signal_type}
              </span>
              <span className={`badge ${displaySignal.status === "approved" || displaySignal.status === "executed" ? "badge-green" : displaySignal.status === "rejected" ? "badge-red" : "badge-muted"}`}>
                {displaySignal.status}
              </span>
              <span style={{ color: "var(--muted)" }}>score {displaySignal.total_score?.toFixed?.(2)}</span>
            </p>
          ) : (
            <p style={{ color: "var(--muted)" }}>No signal yet.</p>
          )}
        </div>

        <div className="panel">
          <h3>Sentiment Score</h3>
          <p style={{ color: "var(--muted)" }}>
            {displaySignal ? displaySignal.sentiment_score?.toFixed?.(2) : "—"}
          </p>
        </div>

        <div className="panel">
          <h3>Market Regime</h3>
          <p style={{ color: "var(--muted)" }}>{displaySignal?.regime ?? "—"}</p>
        </div>
      </div>

      <div className="panel">
        <h3>Open Positions ({positions.length})</h3>
        {positions.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No open positions.</p>
        ) : (
          <table style={{ width: "100%", fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                <th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Unrealized P&L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.id}>
                  <td>{p.symbol}</td>
                  <td>{p.side}</td>
                  <td>{p.qty}</td>
                  <td>{p.entry_price}</td>
                  <td style={{ color: Number(p.unrealized_pnl) >= 0 ? "var(--green)" : "var(--red)" }}>
                    {p.unrealized_pnl}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <div className="panel">
          <h3>Performance / Drawdown</h3>
          {performance ? (
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              <li>Win rate: {performance.win_rate ?? "—"}</li>
              <li>Profit factor: {performance.profit_factor ?? "—"}</li>
              <li>Max drawdown: {performance.max_drawdown ?? "—"}%</li>
              <li>Expectancy (avg R): {performance.expectancy ?? "—"}</li>
            </ul>
          ) : (
            <p style={{ color: "var(--muted)" }}>No performance data yet (populated after the first daily review).</p>
          )}
        </div>

        <div className="panel">
          <h3>Kill Switch / Risk State</h3>
          <p>
            <span className={`status-dot ${killSwitchActive ? "bad" : "ok"}`} />
            {killSwitchActive ? "ACTIVE — entries halted" : "Inactive — normal operation"}
          </p>
          {riskEvents.length > 0 && (
            <>
              <p style={{ color: "var(--muted)", marginBottom: 4 }}>Recent critical risk events:</p>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                {riskEvents.slice(0, 5).map((e) => (
                  <li key={e.id}>{e.event_type}: {e.message}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <div className="panel">
          <h3>Daily Review Summary</h3>
          {dailyReview ? (
            <>
              <p>{dailyReview.summary}</p>
              <p style={{ fontSize: 12, color: "var(--muted)" }}>
                {dailyReview.approved_flag ? "✅ Approved" : "⏳ Pending human approval"}
              </p>
            </>
          ) : (
            <p style={{ color: "var(--muted)" }}>No review yet -- runs automatically after market close.</p>
          )}
        </div>

        <div className="panel">
          <h3>Drift Alerts / Strategy Version</h3>
          <p style={{ fontSize: 13 }}>
            Active strategy: <strong>{strategyVersion?.version_name ?? "none set"}</strong>
          </p>
          {driftEvents.length === 0 ? (
            <p style={{ color: "var(--muted)" }}>No drift detected.</p>
          ) : (
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
              {driftEvents.slice(0, 5).map((d) => (
                <li key={d.id}>{d.drift_type} on {d.metric_name} [{d.severity}]</li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {latestRejected && (
        <div className="panel">
          <h3>Most Recent Rejected Trade</h3>
          <p>
            {latestRejected.symbol} {latestRejected.signal_type} — {latestRejected.reason}
          </p>
        </div>
      )}

      <PriceAlertsPanel />
    </div>
  );
}
