import { useEffect, useState } from "react";

import { DEFAULT_SYMBOL, SYMBOLS } from "../constants/market.js";
import { endpoints } from "../services/api.js";

const selectStyle = {
  background: "var(--panel)", color: "var(--text)",
  border: "1px solid var(--border)", borderRadius: 6, padding: 6,
};

/**
 * apps.monitoring.PriceAlert -- a trader watching a symbol cross a
 * target price, checked every 2 minutes (apps.monitoring.tasks.
 * check_price_alerts) against real candle data and announced through
 * JARVIS when triggered. Not a manual-specified feature (see the
 * model's own docstring) -- a standard trading-platform capability
 * this codebase didn't have at all until now.
 */
export default function PriceAlertsPanel() {
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [condition, setCondition] = useState("above");
  const [targetPrice, setTargetPrice] = useState("");
  const [error, setError] = useState(null);

  const load = () => {
    endpoints.priceAlerts().then((res) => {
      setAlerts(res.data.results ?? []);
      setLoading(false);
    });
  };

  useEffect(() => {
    load();
  }, []);

  const create = async (e) => {
    e.preventDefault();
    setError(null);
    if (!targetPrice) return;
    try {
      await endpoints.createPriceAlert(symbol, condition, targetPrice);
      setTargetPrice("");
      load();
    } catch {
      setError("Could not create the alert -- check the price value and try again.");
    }
  };

  const remove = async (id) => {
    await endpoints.deletePriceAlert(id);
    load();
  };

  const active = alerts.filter((a) => a.active && !a.triggered_at);
  const triggered = alerts.filter((a) => a.triggered_at).slice(0, 5);

  return (
    <div className="panel">
      <h3>Price Alerts</h3>

      <form onSubmit={create} style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={selectStyle}>
          {SYMBOLS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
        <select value={condition} onChange={(e) => setCondition(e.target.value)} style={selectStyle}>
          <option value="above">crosses above</option>
          <option value="below">crosses below</option>
        </select>
        <input
          type="number" step="0.01" placeholder="Target price"
          value={targetPrice} onChange={(e) => setTargetPrice(e.target.value)}
          style={{ ...selectStyle, width: 120 }}
        />
        <button
          type="submit"
          style={{
            padding: "6px 14px", borderRadius: 6, border: "1px solid var(--accent)",
            background: "transparent", color: "var(--accent)", cursor: "pointer",
          }}
        >
          Set alert
        </button>
      </form>
      {error && <p style={{ color: "var(--red)", fontSize: 12 }}>{error}</p>}

      {loading ? (
        <p style={{ color: "var(--muted)" }}>Loading…</p>
      ) : (
        <>
          {active.length === 0 ? (
            <p style={{ color: "var(--muted)", fontSize: 13 }}>No active alerts.</p>
          ) : (
            <ul style={{ margin: 0, paddingLeft: 0, listStyle: "none", fontSize: 13 }}>
              {active.map((a) => (
                <li
                  key={a.id}
                  style={{
                    display: "flex", justifyContent: "space-between", alignItems: "center",
                    padding: "4px 0", borderTop: "1px solid var(--border)",
                  }}
                >
                  <span>{a.symbol} {a.condition} {a.target_price}</span>
                  <button
                    onClick={() => remove(a.id)}
                    title="Cancel alert"
                    style={{ background: "none", border: "none", color: "var(--muted)", cursor: "pointer" }}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}

          {triggered.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 4 }}>Recently triggered</div>
              <ul style={{ margin: 0, paddingLeft: 0, listStyle: "none", fontSize: 12, color: "var(--muted)" }}>
                {triggered.map((a) => (
                  <li key={a.id}>
                    {a.symbol} {a.condition} {a.target_price} — hit {a.triggered_price} at{" "}
                    {new Date(a.triggered_at).toLocaleString()}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}
