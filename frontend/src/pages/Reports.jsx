import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";

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

/**
 * apps.analytics.views.DailyPnLReportView -- one row per trading day
 * (net rupee P&L, trade count, win rate, profit factor, max drawdown).
 * Today's own row is always freshly recomputed server-side (its trading
 * day isn't over yet), everything before it reads the stored, never-
 * recomputed PerformanceMetrics table -- see that view's own docstring.
 */
export default function Reports() {
  const [days, setDays] = useState(30);
  const [rows, setRows] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    endpoints
      .dailyPnLReport(days)
      .then((res) => {
        setRows(res.data.results ?? []);
        setSummary(res.data.summary ?? null);
      })
      .finally(() => setLoading(false));
  }, [days]);

  const daysWithTrades = rows.filter((r) => r.total_trades > 0);

  return (
    <>
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 style={{ margin: 0 }}>Daily P&amp;L Report</h3>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {DAY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>

        {summary && (
          <div style={{ display: "flex", gap: 24, marginTop: 12 }}>
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
          </div>
        )}
      </div>

      <div className="panel">
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
    </>
  );
}
