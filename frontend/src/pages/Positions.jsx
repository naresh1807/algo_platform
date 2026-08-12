import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";

export default function Positions() {
  const [positions, setPositions] = useState([]);

  useEffect(() => {
    endpoints.positions().then((res) => setPositions(res.data.results ?? []));
  }, []);

  return (
    <div className="panel">
      <h3>Open Positions</h3>
      {positions.length === 0 ? (
        <p style={{ color: "var(--muted)" }}>No open positions.</p>
      ) : (
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--muted)" }}>
              <th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Stop-Loss</th>
              <th>Target</th><th>Unrealized P&amp;L</th><th>Opened</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.id} style={{ borderTop: "1px solid var(--border)" }}>
                <td>{p.symbol}</td>
                <td>{p.side}</td>
                <td>{p.qty}</td>
                <td>{p.entry_price}</td>
                <td>
                  {p.stop_loss}
                  {p.trailing_stop_distance != null && (
                    <span
                      title={`Trailing by ${p.trailing_stop_distance} -- peak so far ${p.peak_price}`}
                      style={{ marginLeft: 6, fontSize: 11, color: "var(--accent)" }}
                    >
                      ▲ trailing
                    </span>
                  )}
                </td>
                <td>{p.target_price ?? "—"}</td>
                <td style={{ color: Number(p.unrealized_pnl) >= 0 ? "var(--green)" : "var(--red)" }}>
                  {p.unrealized_pnl}
                </td>
                <td>{new Date(p.opened_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
