import { useEffect, useState } from "react";

import EquityCurveChart from "../charts/EquityCurveChart.jsx";
import { endpoints } from "../services/api.js";
import { useThemeStore } from "../store/themeStore.js";

const DAY_OPTIONS = [
  { value: 7, label: "Last 7 days" },
  { value: 30, label: "Last 30 days" },
  { value: 90, label: "Last 90 days" },
];

function formatMoney(value) {
  if (value == null) return "—";
  const n = Number(value);
  return (n >= 0 ? "+" : "") + n.toFixed(2);
}

function formatPct(value, digits = 0) {
  return value != null ? `${(value * 100).toFixed(digits)}%` : "—";
}

function formatRatio(value) {
  return value != null ? Number(value).toFixed(2) : "—";
}

/**
 * A small "group -> stats" table shared by every breakdown section below
 * (regime/expiry/option-side/strategy/time-of-day) -- all six of
 * apps.analytics.services' breakdown functions return the exact same
 * shape (trade_count/win_rate/net_pnl/avg_r/profit_factor) alongside one
 * dimension-specific key, so one renderer covers all of them.
 */
function BreakdownTable({ rows, groupKey, groupLabel }) {
  if (!rows || rows.length === 0) {
    return <p style={{ color: "var(--muted)", fontSize: 13 }}>No closed trades yet for this breakdown.</p>;
  }
  return (
    <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
      <thead>
        <tr style={{ textAlign: "left", color: "var(--muted)" }}>
          <th>{groupLabel}</th><th>Trades</th><th>Win Rate</th><th>Net P&amp;L</th>
          <th>Avg R</th><th>Profit Factor</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r[groupKey]} style={{ borderTop: "1px solid var(--border)" }}>
            <td>{r[groupKey]}</td>
            <td>{r.trade_count}</td>
            <td>{formatPct(r.win_rate)}</td>
            <td style={{ color: Number(r.net_pnl) >= 0 ? "var(--green)" : "var(--red)", fontWeight: 600 }}>
              {formatMoney(r.net_pnl)}
            </td>
            <td>{formatRatio(r.avg_r)}</td>
            <td>{formatRatio(r.profit_factor)}</td>
          </tr>
        ))}
      </tbody>
    </table>
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

  return (
    <>
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 style={{ margin: 0 }}>Performance Dashboard</h3>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {DAY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>

        {summary && (
          <div style={{ display: "flex", gap: 24, marginTop: 12, flexWrap: "wrap" }}>
            <div>
              <div style={{ fontSize: 12, color: "var(--muted)" }}>Net P&amp;L ({days}d)</div>
              <div
                style={{
                  fontSize: 24, fontWeight: 700,
                  color: Number(summary.total_net_pnl) >= 0 ? "var(--green)" : "var(--red)",
                }}
              >
                {formatMoney(summary.total_net_pnl)}
              </div>
            </div>
            <div>
              <div style={{ fontSize: 12, color: "var(--muted)" }}>Total trades</div>
              <div style={{ fontSize: 24, fontWeight: 700 }}>{summary.total_trades}</div>
            </div>
            <div>
              <div style={{ fontSize: 12, color: "var(--muted)" }}>Sharpe</div>
              <div style={{ fontSize: 24, fontWeight: 700 }}>{formatRatio(ratios?.sharpe_ratio)}</div>
            </div>
            <div>
              <div style={{ fontSize: 12, color: "var(--muted)" }}>Sortino</div>
              <div style={{ fontSize: 24, fontWeight: 700 }}>{formatRatio(ratios?.sortino_ratio)}</div>
            </div>
            <div>
              <div style={{ fontSize: 12, color: "var(--muted)" }}>Calmar</div>
              <div style={{ fontSize: 24, fontWeight: 700 }}>{formatRatio(ratios?.calmar_ratio)}</div>
            </div>
            <div>
              <div style={{ fontSize: 12, color: "var(--muted)" }}>No-Trade Rate</div>
              <div style={{ fontSize: 24, fontWeight: 700 }}>{formatPct(breakdown?.no_trade?.no_trade_rate)}</div>
            </div>
          </div>
        )}
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>Equity Curve</h3>
        {equityPoints.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No equity history in this window yet.</p>
        ) : (
          <EquityCurveChart points={equityPoints} theme={theme} />
        )}
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>Daily P&amp;L</h3>
        {loading ? (
          <p style={{ color: "var(--muted)" }}>Loading…</p>
        ) : daysWithTrades.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>
            No closed trades in the last {days} days yet.
          </p>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                <th>Date</th><th>Net P&amp;L</th><th>Trades</th><th>Win Rate</th>
                <th>Profit Factor</th><th>Expectancy (R)</th><th>Max Drawdown</th>
              </tr>
            </thead>
            <tbody>
              {daysWithTrades.map((r) => (
                <tr key={r.date} style={{ borderTop: "1px solid var(--border)" }}>
                  <td>{r.date}</td>
                  <td style={{ color: Number(r.net_pnl) >= 0 ? "var(--green)" : "var(--red)", fontWeight: 600 }}>
                    {formatMoney(r.net_pnl)}
                  </td>
                  <td>{r.total_trades}</td>
                  <td>{r.win_rate != null ? `${(r.win_rate * 100).toFixed(0)}%` : "—"}</td>
                  <td>{r.profit_factor != null ? r.profit_factor.toFixed(2) : "—"}</td>
                  <td>{r.expectancy != null ? r.expectancy.toFixed(2) : "—"}</td>
                  <td>{r.max_drawdown != null ? `${r.max_drawdown.toFixed(2)}%` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>By Strategy</h3>
        <BreakdownTable rows={breakdown?.by_strategy} groupKey="strategy" groupLabel="Strategy" />
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>By Regime</h3>
        <BreakdownTable rows={breakdown?.by_regime} groupKey="regime" groupLabel="Regime" />
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>By Option Side</h3>
        <BreakdownTable rows={breakdown?.by_option_side} groupKey="option_side" groupLabel="Side" />
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>By Expiry</h3>
        <BreakdownTable rows={breakdown?.by_expiry} groupKey="expiry" groupLabel="Expiry" />
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>By Time of Day</h3>
        <BreakdownTable rows={breakdown?.by_time_of_day} groupKey="phase" groupLabel="Session Phase" />
      </div>
    </>
  );
}
