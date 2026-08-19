import { useEffect, useState } from "react";
import {
  Activity, AlertTriangle, Bot, ListChecks, PieChart, RadioTower, ShieldAlert,
  ShieldCheck, TrendingDown, TrendingUp, Wallet, Wifi,
} from "lucide-react";

import MACDPanel from "../charts/MACDPanel.jsx";
import PriceChart from "../charts/PriceChart.jsx";
import RSIPanel from "../charts/RSIPanel.jsx";
import PriceAlertsPanel from "../components/PriceAlertsPanel.jsx";
import IndexTickerBar from "../components/IndexTickerBar.jsx";
import MarketOutlookPanel from "../components/MarketOutlookPanel.jsx";
import MetricCard from "../components/MetricCard.jsx";
import EmptyState from "../components/EmptyState.jsx";
import ErrorState from "../components/ErrorState.jsx";
import LoadingSkeleton from "../components/LoadingSkeleton.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import { DEFAULT_SYMBOL, DEFAULT_TIMEFRAME, SYMBOLS, TIMEFRAMES } from "../constants/market.js";
import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";
import { useThemeStore } from "../store/themeStore.js";

function formatMoney(value) {
  if (value === null || value === undefined) return null;
  return `₹${Number(value).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

/**
 * This page's panel list is a direct 1:1 mapping of manual section 18
 * ("Dashboard Requirements") -- chart, indicators, signal score,
 * sentiment score, regime status, risk state, open positions, P&L,
 * drawdown, daily review notes, drift alerts, strategy version,
 * kill-switch state. Rejected-trade reasons (section 8) live in the
 * Signals page rather than duplicated here, since that list can get
 * long -- this page only shows the most recent one as a teaser.
 *
 * The summary-card row's "Capital Deployed"/"Exposure" figures are
 * real, computed from `positions` (sum of entry_price*qty for open
 * positions) -- there is no broker "margin" concept in this backend
 * (the platform only ever buys option premium outright, never sells/
 * writes options, so there's nothing that requires margin beyond the
 * premium itself); "Used Margin" from the spec is deliberately relabeled
 * rather than faked. "Active Orders" has no dedicated backend model
 * either -- signals with status pending/approved (not yet resolved to
 * executed/rejected) are the closest real equivalent, since approval
 * auto-executes in this fully-automated platform.
 *
 * Every panel reads real values from state that starts `null`/`[]` and
 * a loading flag, rather than showing fabricated placeholder numbers --
 * same "don't fake it" principle used throughout this codebase.
 */
export default function Dashboard() {
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [timeframe, setTimeframe] = useState(DEFAULT_TIMEFRAME);
  const [candles, setCandles] = useState([]);
  const [candlesLoading, setCandlesLoading] = useState(true);
  const [candlesError, setCandlesError] = useState(null);
  const [candlesRetryToken, setCandlesRetryToken] = useState(0);
  const [indicators, setIndicators] = useState([]);
  const [signals, setSignals] = useState([]);
  const [positions, setPositions] = useState([]);
  const [performance, setPerformance] = useState(null);
  const [dailyReview, setDailyReview] = useState(null);
  const [driftEvents, setDriftEvents] = useState([]);
  const [strategyVersion, setStrategyVersion] = useState(null);
  const [riskEvents, setRiskEvents] = useState([]);
  const [marketSummary, setMarketSummary] = useState(null);
  const [equity, setEquity] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);

  const latestSignal = useLiveStore((s) => s.latestSignal);
  const latestCandle = useLiveStore((s) => s.latestCandle);
  const killSwitchActive = useLiveStore((s) => s.killSwitchActive);
  const connected = useLiveStore((s) => s.connected);
  const feedHealthy = useLiveStore((s) => s.feedHealthy);
  const theme = useThemeStore((s) => s.theme);

  const loadAll = () => {
    setLoading(true);
    setLoadError(null);
    Promise.allSettled([
      endpoints.signals({}),
      endpoints.positions(),
      endpoints.performance(),
      endpoints.dailyReviews(),
      endpoints.driftEvents(),
      endpoints.strategyVersions(),
      endpoints.riskEvents({ severity: "critical" }),
      endpoints.marketSummary(),
      endpoints.equity(),
    ]).then(
      ([
        signalsRes, positionsRes, perfRes, reviewRes, driftRes,
        strategyRes, riskRes, summaryRes, equityRes,
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
        if (equityRes.status === "fulfilled") setEquity(equityRes.value.data);

        const allFailed = [
          signalsRes, positionsRes, perfRes, reviewRes, driftRes, strategyRes, riskRes, equityRes,
        ].every((r) => r.status === "rejected");
        if (allFailed) {
          setLoadError("Could not reach the backend API. Is Django running on 127.0.0.1:8000?");
        }
        setLoading(false);
      }
    );
  };

  useEffect(loadAll, []);

  useEffect(() => {
    let cancelled = false;
    setCandlesLoading(true);
    setCandlesError(null);
    endpoints
      .candles(symbol, timeframe)
      .then((res) => {
        if (!cancelled) setCandles(res.data.results ?? []);
      })
      .catch((err) => {
        if (cancelled) return;
        // A genuine fetch failure (timeout, network error, 5xx) is NOT
        // the same as "no candles ingested yet" -- collapsing both into
        // an empty candles array used to make a real error look
        // identical to an ordinary cold-start empty state, with no way
        // to tell the two apart or retry. axios's own ECONNABORTED code
        // marks a timeout specifically (see services/api.js's new
        // REQUEST_TIMEOUT_MS) so that case gets its own clear message.
        setCandles([]);
        setCandlesError(
          err.code === "ECONNABORTED"
            ? "The request timed out. The backend may be under load -- try again."
            : "Could not load candle data."
        );
      })
      .finally(() => {
        if (!cancelled) setCandlesLoading(false);
      });

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
  }, [symbol, timeframe, candlesRetryToken]);

  const liveCandle =
    !candlesLoading && latestCandle && latestCandle.symbol === symbol && latestCandle.timeframe === timeframe
      ? latestCandle
      : null;

  const latestRejected = signals.find((s) => s.status === "rejected");
  const displaySignal = latestSignal ?? signals[0] ?? null;

  const capitalDeployed = positions.reduce(
    (sum, p) => sum + Number(p.entry_price ?? 0) * Number(p.qty ?? 0), 0,
  );
  const exposurePct = equity?.current_equity
    ? (capitalDeployed / Number(equity.current_equity)) * 100
    : null;
  const activeOrders = signals.filter((s) => s.status === "pending" || s.status === "approved").length;
  const dailyPnlAmount = equity && equity.daily_pnl_pct !== undefined
    ? Number(equity.current_equity) - Number(equity.daily_start_equity ?? equity.current_equity)
    : null;

  let riskTone = "ok";
  let riskLabel = "Normal";
  if (killSwitchActive) {
    riskTone = "bad";
    riskLabel = "Halted";
  } else if (equity?.drawdown_pct >= 10) {
    riskTone = "warn";
    riskLabel = "Elevated";
  }

  return (
    <div>
      {loadError && <ErrorState title="Couldn't load the dashboard" detail={loadError} onRetry={loadAll} />}

      <div className="metric-grid">
        <MetricCard
          title="Available Balance"
          icon={Wallet}
          loading={loading}
          value={formatMoney(equity?.current_equity)}
        />
        <MetricCard
          title="Capital Deployed"
          icon={PieChart}
          loading={loading}
          value={formatMoney(capitalDeployed)}
          sub={positions.length > 0 ? `across ${positions.length} position${positions.length === 1 ? "" : "s"}` : undefined}
        />
        <MetricCard
          title="Today's P&L"
          icon={dailyPnlAmount >= 0 ? TrendingUp : TrendingDown}
          loading={loading}
          tone={dailyPnlAmount === null ? undefined : dailyPnlAmount >= 0 ? "positive" : "negative"}
          value={dailyPnlAmount === null ? null : `${dailyPnlAmount >= 0 ? "+" : ""}${formatMoney(dailyPnlAmount)}`}
          sub={equity?.daily_pnl_pct !== undefined ? `${equity.daily_pnl_pct >= 0 ? "+" : ""}${equity.daily_pnl_pct.toFixed(2)}%` : undefined}
        />
        <MetricCard
          title="Open Positions"
          icon={Activity}
          loading={loading}
          value={positions.length}
        />
        <MetricCard
          title="Active Signals"
          icon={ListChecks}
          loading={loading}
          value={activeOrders}
          sub="pending or approved, not yet resolved"
        />
        <MetricCard
          title="Exposure"
          icon={PieChart}
          loading={loading}
          value={exposurePct === null ? null : `${exposurePct.toFixed(1)}%`}
          sub="of available balance"
        />
        <MetricCard
          title="AI Signals Today"
          icon={Bot}
          loading={loading}
          value={signals.length}
        />
        <MetricCard
          title="Risk Status"
          icon={killSwitchActive ? ShieldAlert : ShieldCheck}
          loading={loading}
          tone={riskTone === "ok" ? undefined : riskTone === "warn" ? "warn" : "negative"}
          value={riskLabel}
        />
      </div>

      <IndexTickerBar />

      <MarketOutlookPanel />

      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 style={{ margin: 0 }}>
            Live Chart — {symbol} ({TIMEFRAMES.find((t) => t.value === timeframe)?.label ?? timeframe})
          </h3>
          <div style={{ display: "flex", gap: 8 }}>
            <select className="input" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              {SYMBOLS.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
            <select className="input" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
              {TIMEFRAMES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>
        </div>
        {candlesLoading ? (
          <LoadingSkeleton height={420} />
        ) : candlesError ? (
          <ErrorState
            title="Couldn't load the chart"
            detail={candlesError}
            onRetry={() => setCandlesRetryToken((t) => t + 1)}
          />
        ) : candles.length === 0 ? (
          <EmptyState
            title={`No ${symbol} ${timeframe} candles yet`}
            detail="Run the ingestion task (BROKER_MODE=live) or the backfill command to populate candle history."
          />
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
        {marketSummary ? (
          <p style={{ margin: 0 }}>{marketSummary}</p>
        ) : (
          <EmptyState title="No market summary yet" detail="Populated once today's news polling has run." />
        )}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }} className="two-col-grid">
        <div className="panel">
          <h3>Latest Signal</h3>
          {displaySignal ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <span style={{ fontWeight: 700 }}>{displaySignal.symbol}</span>
                <StatusBadge tone={displaySignal.signal_type === "buy" ? "ok" : displaySignal.signal_type === "sell" ? "bad" : "muted"}>
                  {displaySignal.signal_type}
                </StatusBadge>
                {displaySignal.option_side && (
                  <StatusBadge tone="muted">
                    {displaySignal.strike_price ? `${displaySignal.strike_price} ` : ""}{displaySignal.option_side}
                  </StatusBadge>
                )}
                <StatusBadge tone={displaySignal.status === "approved" || displaySignal.status === "executed" ? "ok" : displaySignal.status === "rejected" ? "bad" : "muted"}>
                  {displaySignal.status}
                </StatusBadge>
              </div>
              <span style={{ color: "var(--muted)", fontSize: 12.5 }}>
                Score {displaySignal.total_score?.toFixed?.(2)} · Sentiment {displaySignal.sentiment_score?.toFixed?.(2) ?? "—"} · {displaySignal.regime ?? "—"}
              </span>
            </div>
          ) : (
            <EmptyState title="No signal yet" />
          )}
        </div>

        <div className="panel">
          <h3>System Health</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span><StatusBadge tone={connected ? "ok" : "warn"} icon={Wifi}>{connected ? "WebSocket Live" : "Reconnecting"}</StatusBadge></span>
            <span><StatusBadge tone={feedHealthy ? "ok" : "bad"} icon={RadioTower}>Broker feed {feedHealthy ? "healthy" : "degraded"}</StatusBadge></span>
            <span><StatusBadge tone={loadError ? "bad" : "ok"}>API {loadError ? "unreachable" : "reachable"}</StatusBadge></span>
          </div>
        </div>

        <div className="panel">
          <h3>Kill Switch / Risk State</h3>
          <p style={{ marginTop: 0 }}>
            <StatusBadge tone={killSwitchActive ? "bad" : "ok"} icon={killSwitchActive ? ShieldAlert : ShieldCheck}>
              {killSwitchActive ? "ACTIVE — entries halted" : "Inactive — normal operation"}
            </StatusBadge>
          </p>
          {riskEvents.length > 0 && (
            <>
              <p style={{ color: "var(--muted)", marginBottom: 4, fontSize: 12.5 }}>Recent critical risk events:</p>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                {riskEvents.slice(0, 5).map((e) => (
                  <li key={e.id}>{e.event_type}: {e.message}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>

      <div className="panel">
        <h3>Open Positions ({positions.length})</h3>
        {positions.length === 0 ? (
          <EmptyState title="No open positions" detail="Approved signals open real (or paper) positions automatically." />
        ) : (
          <div className="data-table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Unrealized P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.id}>
                    <td>{p.symbol}</td>
                    <td>{p.side}</td>
                    <td className="num">{p.qty}</td>
                    <td className="num">{p.entry_price}</td>
                    <td className={Number(p.unrealized_pnl) >= 0 ? "num-positive" : "num-negative"}>
                      {p.unrealized_pnl}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }} className="two-col-grid">
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
            <EmptyState title="No performance data yet" detail="Populated after the first daily review." />
          )}
        </div>

        <div className="panel">
          <h3>Daily Review Summary</h3>
          {dailyReview ? (
            <>
              <p style={{ marginTop: 0 }}>{dailyReview.summary}</p>
              <StatusBadge tone={dailyReview.approved_flag ? "ok" : "muted"}>
                {dailyReview.approved_flag ? "Approved" : "Pending human approval"}
              </StatusBadge>
            </>
          ) : (
            <EmptyState title="No review yet" detail="Runs automatically after market close." />
          )}
        </div>
      </div>

      <div className="panel">
        <h3>Drift Alerts / Strategy Version</h3>
        <p style={{ fontSize: 13 }}>
          Active strategy: <strong>{strategyVersion?.version_name ?? "none set"}</strong>
        </p>
        {driftEvents.length === 0 ? (
          <p style={{ color: "var(--muted)", margin: 0 }}>No drift detected.</p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            {driftEvents.slice(0, 5).map((d) => (
              <li key={d.id}>{d.drift_type} on {d.metric_name} [{d.severity}]</li>
            ))}
          </ul>
        )}
      </div>

      {latestRejected && (
        <div className="panel">
          <h3><AlertTriangle size={13} style={{ verticalAlign: -2, marginRight: 4 }} />Most Recent Rejected Trade</h3>
          <p style={{ margin: 0 }}>
            {latestRejected.symbol} {latestRejected.signal_type} — {latestRejected.reason}
          </p>
        </div>
      )}

      <PriceAlertsPanel />
    </div>
  );
}
