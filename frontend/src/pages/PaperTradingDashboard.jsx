import { useEffect, useState } from "react";
import {
  Activity, AlertTriangle, Bot, Brain, ShieldAlert, Sparkles, Wallet, Wifi,
} from "lucide-react";

import DataTable from "../components/DataTable.jsx";
import EmptyState from "../components/EmptyState.jsx";
import ErrorState from "../components/ErrorState.jsx";
import LoadingSkeleton from "../components/LoadingSkeleton.jsx";
import MetricCard from "../components/MetricCard.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";

function formatMoney(value) {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return `${n < 0 ? "-" : ""}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function formatPct(value) {
  if (value === null || value === undefined) return null;
  return `${Number(value).toFixed(2)}%`;
}

function pnlTone(value) {
  if (value === null || value === undefined) return undefined;
  const n = Number(value);
  return n > 0 ? "positive" : n < 0 ? "negative" : undefined;
}

const ORDER_COLUMNS = [
  { key: "requested_at", label: "Time", render: (r) => new Date(r.requested_at).toLocaleTimeString() },
  { key: "contract_label", label: "Contract" },
  { key: "side", label: "Side" },
  { key: "quantity", label: "Qty", align: "right" },
  { key: "status", label: "Status", render: (r) => <StatusBadge tone={r.status === "filled" ? "ok" : r.status === "rejected" ? "bad" : "muted"}>{r.status}</StatusBadge> },
  { key: "average_fill_price", label: "Fill Price", align: "right", render: (r) => formatMoney(r.average_fill_price) ?? "—" },
];

const TRADE_COLUMNS = [
  { key: "closed_at", label: "Closed", render: (r) => new Date(r.closed_at).toLocaleString() },
  { key: "entry_price", label: "Entry", align: "right", render: (r) => formatMoney(r.entry_price) },
  { key: "exit_price", label: "Exit", align: "right", render: (r) => formatMoney(r.exit_price) },
  { key: "quantity", label: "Qty", align: "right" },
  {
    key: "net_pnl", label: "Net P&L", align: "right",
    render: (r) => <span className={pnlTone(r.net_pnl)}>{formatMoney(r.net_pnl)}</span>,
  },
  {
    key: "outcome", label: "Outcome",
    render: (r) => r.result?.outcome_classification
      ? <StatusBadge tone={r.net_pnl >= 0 ? "ok" : "bad"}>{r.result.outcome_classification.replaceAll("_", " ")}</StatusBadge>
      : "—",
  },
];

/**
 * Read-only monitoring dashboard for apps.paper_trading -- the
 * autonomous AI paper-trading subsystem (a fully separate vertical
 * from the Positions/Reports pages, which cover apps.execution's
 * shared paper/live pipeline). Deliberately has NO Buy/Sell/Modify/
 * Cancel/Square-off controls anywhere: there is no mutating endpoint
 * in apps.paper_trading's API for this page to call, by construction
 * (see apps/paper_trading/views.py's own module docstring) -- the user
 * monitors capital, positions, and P&L; the AI decides everything.
 *
 * Follows Dashboard.jsx's own template: one Promise.allSettled REST
 * load on mount, then a live WebSocket push (useLiveStore's
 * paperAccount/paperPosition/paperLatestDecision fields, wired in
 * App.jsx at the AppShell level, same as every other socket) merged in
 * defensively -- the live value is preferred once it arrives, but the
 * page never blocks on it.
 */
export default function PaperTradingDashboard() {
  const [account, setAccount] = useState(null);
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [trades, setTrades] = useState([]);
  const [dailySummaries, setDailySummaries] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const killSwitchActive = useLiveStore((s) => s.killSwitchActive);
  const feedHealthy = useLiveStore((s) => s.feedHealthy);
  const liveAccount = useLiveStore((s) => s.paperAccount);
  const livePosition = useLiveStore((s) => s.paperPosition);
  const liveDecision = useLiveStore((s) => s.paperLatestDecision);

  const load = () => {
    setLoading(true);
    setError(null);
    Promise.allSettled([
      endpoints.paperAccount(),
      endpoints.paperPositions({ status: "open" }),
      endpoints.paperOrders(),
      endpoints.paperTrades(),
      endpoints.paperDailySummaries(),
      endpoints.paperDecisions(),
    ]).then(([accountRes, positionsRes, ordersRes, tradesRes, summariesRes, decisionsRes]) => {
      setLoading(false);
      if (accountRes.status === "fulfilled") setAccount(accountRes.value.data);
      else setError("Could not load the paper account -- is the backend running?");
      if (positionsRes.status === "fulfilled") setPositions(positionsRes.value.data.results ?? []);
      if (ordersRes.status === "fulfilled") setOrders(ordersRes.value.data.results ?? []);
      if (tradesRes.status === "fulfilled") setTrades(tradesRes.value.data.results ?? []);
      if (summariesRes.status === "fulfilled") setDailySummaries(summariesRes.value.data.results ?? []);
      if (decisionsRes.status === "fulfilled") setDecisions(decisionsRes.value.data.results ?? []);
    });
  };

  useEffect(load, []);

  // Live push takes precedence once it arrives, falling back to the
  // last REST-loaded value -- same pattern Dashboard.jsx's own
  // `displaySignal = latestSignal ?? signals[0] ?? null` uses.
  const displayAccount = liveAccount ?? account;
  const openPosition = livePosition?.status === "open" ? livePosition : (positions[0] ?? null);
  const latestDecision = liveDecision ?? decisions[0] ?? null;
  const latestSummary = dailySummaries[0] ?? null;

  if (loading) return <LoadingSkeleton rows={8} />;
  if (error) return <ErrorState detail={error} onRetry={load} />;

  return (
    <div>
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 8 }}>
            <Bot size={16} style={{ color: "var(--jarvis)" }} />
            AI Paper Trading
            <StatusBadge tone="jarvis">PAPER TRADING</StatusBadge>
          </h3>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <StatusBadge tone={feedHealthy ? "ok" : "bad"} icon={Wifi}>
              Angel One data {feedHealthy ? "live" : "degraded"}
            </StatusBadge>
            <StatusBadge tone={displayAccount?.session_state === "error_safe_mode" ? "bad" : "info"} icon={Activity}>
              {(displayAccount?.session_state ?? "flat").replaceAll("_", " ")}
            </StatusBadge>
            <StatusBadge tone={displayAccount?.champion_model_version ? "ok" : "muted"} icon={Brain}>
              Model {displayAccount?.champion_model_version ?? "heuristic (no trained model yet)"}
            </StatusBadge>
            {killSwitchActive && (
              <StatusBadge tone="bad" icon={ShieldAlert}>Kill switch ACTIVE</StatusBadge>
            )}
          </div>
        </div>
        <p style={{ color: "var(--muted)", fontSize: 12.5, marginTop: 8, marginBottom: 0 }}>
          Fully autonomous -- the AI decides every entry, exit, and stop adjustment. This page is
          read-only monitoring; there are no manual trade controls here by design.
        </p>
      </div>

      <div className="card-grid" style={{ marginTop: 12 }}>
        <MetricCard title="Initial Capital" value={formatMoney(displayAccount?.initial_capital)} icon={Wallet} />
        <MetricCard title="Available Cash" value={formatMoney(displayAccount?.available_cash)} icon={Wallet} />
        <MetricCard title="Used Capital" value={formatMoney(displayAccount?.used_capital)} />
        <MetricCard title="Current Equity" value={formatMoney(displayAccount?.current_equity)} />
        <MetricCard title="Realized P&L" value={formatMoney(displayAccount?.realized_pnl)} tone={pnlTone(displayAccount?.realized_pnl)} />
        <MetricCard title="Unrealized P&L" value={formatMoney(displayAccount?.unrealized_pnl)} tone={pnlTone(displayAccount?.unrealized_pnl)} />
        <MetricCard title="Gross P&L" value={formatMoney(displayAccount?.gross_pnl)} tone={pnlTone(displayAccount?.gross_pnl)} />
        <MetricCard title="Total Charges" value={formatMoney(displayAccount?.total_charges)} />
        <MetricCard title="Net P&L" value={formatMoney(displayAccount?.net_pnl)} tone={pnlTone(displayAccount?.net_pnl)} />
        <MetricCard title="Daily Drawdown" value={formatPct(displayAccount?.daily_drawdown)} tone={displayAccount?.daily_drawdown > 0 ? "warn" : undefined} />
        <MetricCard title="Max Drawdown" value={formatPct(displayAccount?.max_drawdown)} tone={displayAccount?.max_drawdown > 0 ? "warn" : undefined} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
        <div className="panel">
          <h3>Current Position</h3>
          {openPosition ? (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 10, fontSize: 13 }}>
              <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Contract</div>{openPosition.contract_label ?? openPosition.contract}</div>
              <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Quantity</div>{openPosition.quantity}</div>
              <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Entry Price</div>{formatMoney(openPosition.average_entry_price)}</div>
              <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Mark Price</div>{formatMoney(openPosition.last_mark_price) ?? "—"}</div>
              <div><div style={{ color: "var(--red)", fontSize: 11 }}>Stop Loss</div>{formatMoney(openPosition.stop_loss)}</div>
              <div><div style={{ color: "var(--green)", fontSize: 11 }}>Target</div>{formatMoney(openPosition.target_price) ?? "—"}</div>
              <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Trailing Stop</div>{formatMoney(openPosition.trailing_stop_distance) ?? "off"}</div>
              <div>
                <div style={{ color: "var(--muted)", fontSize: 11 }}>Unrealized P&L</div>
                <span className={pnlTone(openPosition.unrealized_pnl)}>{formatMoney(openPosition.unrealized_pnl) ?? "—"}</span>
              </div>
            </div>
          ) : (
            <EmptyState title="No open position" detail="The AI is flat -- watching for a qualifying entry." />
          )}
        </div>

        <div className="panel">
          <h3><Sparkles size={14} style={{ verticalAlign: -2, marginRight: 4 }} />Latest AI Action</h3>
          {latestDecision ? (
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                <StatusBadge tone={latestDecision.action?.startsWith("buy") ? "ok" : latestDecision.action === "exit_position" ? "bad" : "muted"}>
                  {latestDecision.action?.replaceAll("_", " ")}
                </StatusBadge>
                {latestDecision.confidence != null && (
                  <span style={{ fontSize: 12, color: "var(--muted)" }}>
                    confidence {(Number(latestDecision.confidence) * 100).toFixed(0)}%
                  </span>
                )}
              </div>
              <p style={{ fontSize: 12.5, color: "var(--text)", margin: 0 }}>
                {latestDecision.risk_engine_response_json?.reason ?? latestDecision.reason ?? "—"}
              </p>
              {latestDecision.timestamp && (
                <p style={{ fontSize: 11, color: "var(--muted)", marginTop: 6, marginBottom: 0 }}>
                  {new Date(latestDecision.timestamp).toLocaleString()}
                </p>
              )}
            </div>
          ) : (
            <EmptyState title="No decisions recorded yet" />
          )}
        </div>
      </div>

      <div className="panel" style={{ marginTop: 12 }}>
        <h3>Recent Paper Orders</h3>
        <DataTable
          columns={ORDER_COLUMNS} rows={orders} getRowKey={(r) => r.id}
          emptyTitle="No orders yet" searchKeys={["contract_label", "status"]}
        />
      </div>

      <div className="panel" style={{ marginTop: 12 }}>
        <h3>Recent Completed Trades</h3>
        <DataTable
          columns={TRADE_COLUMNS} rows={trades} getRowKey={(r) => r.id}
          emptyTitle="No completed trades yet"
        />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
        <div className="panel">
          <h3>Daily Learning Summary</h3>
          {latestSummary ? (
            <div style={{ fontSize: 13 }}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 8 }}>
                <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Trading Day</div>{latestSummary.trading_day}</div>
                <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Trades</div>{latestSummary.trades_count} ({latestSummary.wins}W / {latestSummary.losses}L)</div>
                <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Win Rate</div>{latestSummary.win_rate != null ? `${(latestSummary.win_rate * 100).toFixed(0)}%` : "—"}</div>
                <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Net P&L</div><span className={pnlTone(latestSummary.net_pnl)}>{formatMoney(latestSummary.net_pnl)}</span></div>
                <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Profit Factor</div>{latestSummary.profit_factor != null ? Number(latestSummary.profit_factor).toFixed(2) : "—"}</div>
                <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Avg MFE / MAE</div>{formatMoney(latestSummary.avg_mfe) ?? "—"} / {formatMoney(latestSummary.avg_mae) ?? "—"}</div>
              </div>
            </div>
          ) : (
            <EmptyState title="No daily report yet" detail="Generated automatically after market close." />
          )}
        </div>

        <div className="panel">
          <h3><Brain size={14} style={{ verticalAlign: -2, marginRight: 4 }} />Champion / Challenger Model</h3>
          {latestSummary ? (
            <div style={{ fontSize: 13 }}>
              <p style={{ margin: "0 0 6px" }}>
                Champion: <strong>{latestSummary.champion_model_version || "none yet (heuristic policy active)"}</strong>
              </p>
              <StatusBadge tone={latestSummary.promotion_status?.startsWith("promoted") ? "ok" : latestSummary.promotion_status?.startsWith("rejected") ? "warn" : "muted"}>
                {latestSummary.promotion_status || "not evaluated"}
              </StatusBadge>
              {latestSummary.challenger_result_json?.performance && (
                <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 8 }}>
                  Challenger expectancy {Number(latestSummary.challenger_result_json.performance.expectancy_r ?? 0).toFixed(3)}R,
                  {" "}profit factor {Number(latestSummary.challenger_result_json.performance.profit_factor ?? 0).toFixed(2)}
                </p>
              )}
            </div>
          ) : (
            <EmptyState title="No model evaluation yet" icon={AlertTriangle} />
          )}
        </div>
      </div>
    </div>
  );
}
