import { useEffect, useState } from "react";

import EquityCurveChart from "../charts/EquityCurveChart.jsx";
import DataTable from "../components/DataTable.jsx";
import EmptyState from "../components/EmptyState.jsx";
import MetricCard from "../components/MetricCard.jsx";
import { endpoints } from "../services/api.js";
import { useThemeStore } from "../store/themeStore.js";

const DAY_OPTIONS = [
  { value: 7, label: "Last 7 days" },
  { value: 30, label: "Last 30 days" },
  { value: 90, label: "Last 90 days" },
];

function formatMoney(value) {
  if (value == null) return null;
  const n = Number(value);
  return (n >= 0 ? "+" : "") + n.toFixed(2);
}

function formatPct(value, digits = 0) {
  return value != null ? `${(value * 100).toFixed(digits)}%` : null;
}

function formatRatio(value) {
  return value != null ? Number(value).toFixed(2) : null;
}

// Shared by every breakdown table below (regime/expiry/option-side/
// strategy/time-of-day) -- all six of apps.analytics.services'
// breakdown functions return the exact same shape (trade_count/
// win_rate/net_pnl/avg_r/profit_factor) alongside one dimension-
// specific key, so one column set (parameterized by that key/label)
// covers all of them via the shared DataTable component.
function breakdownColumns(groupKey, groupLabel) {
  return [
    { key: groupKey, label: groupLabel },
    { key: "trade_count", label: "Trades", align: "right" },
    { key: "win_rate", label: "Win Rate", align: "right", render: (r) => formatPct(r.win_rate) ?? "—" },
    {
      key: "net_pnl", label: "Net P&L", align: "right",
      render: (r) => <span className={Number(r.net_pnl) >= 0 ? "num-positive" : "num-negative"}>{formatMoney(r.net_pnl) ?? "—"}</span>,
    },
    { key: "avg_r", label: "Avg R", align: "right", render: (r) => formatRatio(r.avg_r) ?? "—" },
    { key: "profit_factor", label: "Profit Factor", align: "right", render: (r) => formatRatio(r.profit_factor) ?? "—" },
  ];
}

function BreakdownPanel({ title, rows, groupKey, groupLabel }) {
  return (
    <div className="panel">
      <h3 style={{ margin: "0 0 8px" }}>{title}</h3>
      <DataTable
        columns={breakdownColumns(groupKey, groupLabel)}
        rows={rows ?? []}
        getRowKey={(r) => r[groupKey]}
        emptyTitle="No closed trades yet for this breakdown"
      />
    </div>
  );
}

/**
 * apps.analytics.views.DailyPnLReportView -- one row per trading day
 * (net rupee P&L, trade count, win rate, profit factor, max drawdown).
 * Today's own row is always freshly recomputed server-side (its trading
 * day isn't over yet), everything before it reads the stored, never-
 * recomputed PerformanceMetrics table -- see that view's own docstring.
 *
 * Extended (Phase G, Options Intelligence Engine) with the equity curve,
 * Sharpe/Sortino/Calmar ratios, and the strategy/regime/expiry/option-
 * side/time-of-day/no-trade breakdowns -- all backed by real closed-trade
 * data, no new data source.
 */
export default function Reports() {
  const theme = useThemeStore((s) => s.theme);
  const [days, setDays] = useState(30);
  const [rows, setRows] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [ratios, setRatios] = useState(null);
  const [equityPoints, setEquityPoints] = useState([]);
  const [breakdown, setBreakdown] = useState(null);

  useEffect(() => {
    setLoading(true);
    endpoints
      .dailyPnLReport(days)
      .then((res) => {
        setRows(res.data.results ?? []);
        setSummary(res.data.summary ?? null);
      })
      .finally(() => setLoading(false));

    endpoints.sharpeRatio(days).then((res) => setRatios(res.data));
    endpoints.equityCurve(days).then((res) => setEquityPoints(res.data.results ?? []));
    endpoints.performanceBreakdown(days).then((res) => setBreakdown(res.data));
  }, [days]);

  const daysWithTrades = rows.filter((r) => r.total_trades > 0);
  const netPnl = summary ? Number(summary.total_net_pnl ?? 0) : null;

  const dailyColumns = [
    { key: "date", label: "Date" },
    {
      key: "net_pnl", label: "Net P&L", align: "right",
      render: (r) => <span className={Number(r.net_pnl) >= 0 ? "num-positive" : "num-negative"}>{formatMoney(r.net_pnl) ?? "—"}</span>,
    },
    { key: "total_trades", label: "Trades", align: "right" },
    { key: "win_rate", label: "Win Rate", align: "right", render: (r) => formatPct(r.win_rate) ?? "—" },
    { key: "profit_factor", label: "Profit Factor", align: "right", render: (r) => formatRatio(r.profit_factor) ?? "—" },
    { key: "expectancy", label: "Expectancy (R)", align: "right", render: (r) => formatRatio(r.expectancy) ?? "—" },
    { key: "max_drawdown", label: "Max Drawdown", align: "right", render: (r) => (r.max_drawdown != null ? `${r.max_drawdown.toFixed(2)}%` : "—") },
  ];

  return (
    <>
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 style={{ margin: 0 }}>Performance Dashboard</h3>
          <select className="input" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {DAY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>

        <div className="metric-grid" style={{ marginTop: 12, marginBottom: 0 }}>
          <MetricCard title={`Net P&L (${days}d)`} value={formatMoney(netPnl)} loading={loading} tone={netPnl == null ? undefined : netPnl >= 0 ? "positive" : "negative"} />
          <MetricCard title="Total Trades" value={summary?.total_trades} loading={loading} />
          <MetricCard title="Sharpe" value={formatRatio(ratios?.sharpe_ratio)} loading={loading} />
          <MetricCard title="Sortino" value={formatRatio(ratios?.sortino_ratio)} loading={loading} />
          <MetricCard title="Calmar" value={formatRatio(ratios?.calmar_ratio)} loading={loading} />
          <MetricCard title="No-Trade Rate" value={formatPct(breakdown?.no_trade?.no_trade_rate)} loading={loading} />
        </div>
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>Equity Curve</h3>
        {equityPoints.length === 0 ? (
          <EmptyState title="No equity history in this window yet" />
        ) : (
          <EquityCurveChart points={equityPoints} theme={theme} />
        )}
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>Daily P&amp;L</h3>
        <DataTable
          columns={dailyColumns}
          rows={daysWithTrades}
          getRowKey={(r) => r.date}
          loading={loading}
          emptyTitle={`No closed trades in the last ${days} days yet`}
        />
      </div>

      <BreakdownPanel title="By Strategy" rows={breakdown?.by_strategy} groupKey="strategy" groupLabel="Strategy" />
      <BreakdownPanel title="By Regime" rows={breakdown?.by_regime} groupKey="regime" groupLabel="Regime" />
      <BreakdownPanel title="By Option Side" rows={breakdown?.by_option_side} groupKey="option_side" groupLabel="Side" />
      <BreakdownPanel title="By Expiry" rows={breakdown?.by_expiry} groupKey="expiry" groupLabel="Expiry" />
      <BreakdownPanel title="By Time of Day" rows={breakdown?.by_time_of_day} groupKey="phase" groupLabel="Session Phase" />
    </>
  );
}
