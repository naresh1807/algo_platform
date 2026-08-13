import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";

// apps.learning.strategy_methods.SCALPING_METHOD_FUNCS -- three
// genuinely different 1-minute scalping ideas, run continuously and
// fully isolated from real trading (never touch real positions/equity,
// see apps/learning/models.py's HypotheticalTrade docstring). Their
// win/loss record is evaluated daily by apps.learning.tasks.
// evaluate_scalping_strategy_methods and the current best performer is
// marked "Best" below -- same reuse of ModelRegistry Learning.jsx's own
// "Strategy Comparison" panel already established for the 5m swing group.
const SCALPING_METHODS = [
  { name: "ema_momentum_scalp", label: "EMA Momentum Scalp" },
  { name: "rsi_extreme_scalp", label: "RSI Extreme Reversal Scalp" },
  { name: "sar_volume_burst_scalp", label: "SAR + Volume Burst Scalp" },
];

function formatIst(isoString) {
  return new Date(isoString).toLocaleString("en-GB", {
    timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }) + " IST";
}

export default function Scalping() {
  const [mlModels, setMlModels] = useState([]);
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = () => {
      Promise.all([
        endpoints.modelRegistry(),
        Promise.all(SCALPING_METHODS.map((m) => endpoints.hypotheticalTrades({ method: m.name }))),
      ])
        .then(([registryRes, tradeResArray]) => {
          setMlModels(registryRes.data.results ?? []);
          const allTrades = tradeResArray.flatMap((r) => r.data.results ?? []);
          allTrades.sort((a, b) => new Date(b.opened_at) - new Date(a.opened_at));
          setTrades(allTrades);
        })
        .finally(() => setLoading(false));
    };
    load();
    // 1-minute scalps move fast -- refresh often enough to actually
    // feel live, matching run_scalping_strategy_comparison's own
    // 1-minute beat cadence.
    const interval = setInterval(load, 20_000);
    return () => clearInterval(interval);
  }, []);

  const openTrades = trades.filter((t) => !t.closed_at);
  const closedTrades = trades.filter((t) => t.closed_at);

  return (
    <>
      <div className="panel">
        <h3>Scalping Strategies</h3>
        <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 0 }}>
          Three genuinely different 1-minute scalping methods run continuously and isolated
          from real trading (never touch real positions/equity) -- tight stops, quick 1-1.5R
          targets, closed out within minutes, not hours. Win/loss record is evaluated daily and
          the current best performer is marked below. See apps.learning.strategy_methods'
          SCALPING_METHOD_FUNCS.
        </p>
        {loading ? (
          <p style={{ color: "var(--muted)" }}>Loading...</p>
        ) : (
          <div className="card-grid">
            {SCALPING_METHODS.map(({ name, label }) => {
              const registryRow = mlModels
                .filter((m) => m.model_name === `strategy_comparison_${name}`)
                .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];
              const metrics = registryRow?.metrics_json;
              const isActive = registryRow?.active_flag;
              const openCount = openTrades.filter((t) => t.method === name).length;
              return (
                <div key={name} className={`card ${isActive ? "card-accent-green" : "card-accent-muted"}`}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span style={{ fontWeight: 600 }}>{label}</span>
                    {isActive && <span className="badge badge-green">Best</span>}
                  </div>
                  {metrics ? (
                    <div style={{ marginTop: 8, fontSize: 12, color: "var(--muted)" }}>
                      <div style={{ fontSize: 20, fontWeight: 700, color: "var(--text)" }}>
                        {(metrics.win_rate * 100).toFixed(0)}% win rate
                      </div>
                      <div style={{ marginTop: 4 }}>{metrics.trade_count} closed trade(s)</div>
                      <div>
                        Avg R: {metrics.avg_r_multiple != null ? `${metrics.avg_r_multiple.toFixed(2)}R` : "n/a"}
                      </div>
                      <div style={{ color: metrics.total_pnl >= 0 ? "var(--green)" : "var(--red)" }}>
                        Total P&amp;L: {metrics.total_pnl >= 0 ? "+" : ""}{metrics.total_pnl.toFixed(2)}
                      </div>
                    </div>
                  ) : (
                    <p style={{ color: "var(--muted)", fontSize: 12, marginTop: 8 }}>
                      Not enough closed scalps yet -- evaluated daily after market close.
                    </p>
                  )}
                  {openCount > 0 && (
                    <div style={{ marginTop: 8, fontSize: 12 }}>
                      <span className="badge badge-muted">{openCount} open now</span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="panel">
        <h3>Open Scalps ({openTrades.length})</h3>
        {openTrades.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No open scalping trades right now.</p>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                <th>Method</th><th>Symbol</th><th>Entry</th><th>Stop</th><th>Target</th><th>Opened</th>
              </tr>
            </thead>
            <tbody>
              {openTrades.map((t) => (
                <tr key={t.id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td>{SCALPING_METHODS.find((m) => m.name === t.method)?.label ?? t.method}</td>
                  <td>{t.symbol}</td>
                  <td>{t.entry_price}</td>
                  <td>{t.stop_loss}</td>
                  <td>{t.target_price ?? "—"}</td>
                  <td style={{ color: "var(--muted)" }}>{formatIst(t.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h3>Recent Closed Scalps</h3>
        {closedTrades.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No closed scalping trades yet.</p>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                <th>Method</th><th>Symbol</th><th>Exit reason</th><th>P&amp;L</th><th>R-multiple</th><th>Closed</th>
              </tr>
            </thead>
            <tbody>
              {closedTrades.slice(0, 30).map((t) => (
                <tr key={t.id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td>{SCALPING_METHODS.find((m) => m.name === t.method)?.label ?? t.method}</td>
                  <td>{t.symbol}</td>
                  <td>{t.exit_reason}</td>
                  <td style={{ color: t.pnl >= 0 ? "var(--green)" : "var(--red)" }}>
                    {t.pnl != null ? (t.pnl >= 0 ? "+" : "") + Number(t.pnl).toFixed(2) : "—"}
                  </td>
                  <td>{t.r_multiple != null ? `${t.r_multiple.toFixed(2)}R` : "—"}</td>
                  <td style={{ color: "var(--muted)" }}>{formatIst(t.closed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
