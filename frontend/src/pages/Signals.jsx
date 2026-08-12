import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";

function formatIst(isoString) {
  return new Date(isoString).toLocaleString("en-GB", {
    timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }) + " IST";
}

/**
 * Full signal history, including rejected ones with their `reason` text
 * -- this is the page the manual's "why a trade happened, why it was
 * rejected" requirement is really about (Dashboard.jsx only teases the
 * single most recent rejection).
 */
export default function Signals() {
  const [signals, setSignals] = useState([]);
  const [statusFilter, setStatusFilter] = useState("");

  useEffect(() => {
    endpoints.signals(statusFilter ? { status: statusFilter } : {}).then((res) => {
      setSignals(res.data.results ?? []);
    });
  }, [statusFilter]);

  return (
    <div className="panel">
      <h3>Signals</h3>
      <div style={{ marginBottom: 12 }}>
        {["", "pending", "approved", "rejected", "executed", "expired"].map((s) => (
          <button
            key={s || "all"}
            onClick={() => setStatusFilter(s)}
            style={{
              marginRight: 6,
              padding: "4px 10px",
              borderRadius: 6,
              border: "1px solid var(--border)",
              background: statusFilter === s ? "var(--accent)" : "transparent",
              color: statusFilter === s ? "#fff" : "var(--text)",
              cursor: "pointer",
            }}
          >
            {s || "all"}
          </button>
        ))}
      </div>

      {signals.length === 0 ? (
        <p style={{ color: "var(--muted)" }}>No signals to show.</p>
      ) : (
        <div className="card-grid">
          {signals.map((s) => {
            const isBuy = s.signal_type === "buy";
            const accent = isBuy ? "green" : "muted";
            const statusBadge =
              s.status === "approved" || s.status === "executed" ? "badge-green"
              : s.status === "rejected" ? "badge-red"
              : "badge-muted";
            return (
              <div key={s.id} className={`card card-accent-${accent}`}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 15, fontWeight: 700 }}>{s.symbol}</span>
                    <span className={`badge ${isBuy ? "badge-green" : "badge-muted"}`}>{s.signal_type}</span>
                  </div>
                  <span className={`badge ${statusBadge}`}>{s.status}</span>
                </div>

                {s.created_at && (
                  <div style={{ marginTop: 4, fontSize: 11, color: "var(--muted)" }}>
                    {formatIst(s.created_at)}
                  </div>
                )}

                <div style={{ marginTop: 10, fontSize: 24, fontWeight: 700, color: isBuy ? "var(--green)" : "var(--text)" }}>
                  {s.total_score?.toFixed?.(2) ?? "—"}
                </div>

                <div style={{ marginTop: 6, fontSize: 12, color: "var(--muted)", display: "flex", gap: 12, flexWrap: "wrap" }}>
                  <span>Options: {s.options_score?.toFixed?.(2) ?? "—"}</span>
                  <span>
                    {/* null until apps.learning.ml_train has trained at least
                        one model (needs MIN_TRAINING_SAMPLES closed trades) --
                        show a dash rather than 0% or blank, so "no model yet"
                        reads differently from "model predicts near-zero". */}
                    ML Win: {s.ml_win_probability != null ? `${(s.ml_win_probability * 100).toFixed(0)}%` : "—"}
                  </span>
                  <span>Regime: {s.regime}</span>
                </div>

                {s.reason && (
                  <div style={{ marginTop: 8, fontSize: 12, color: "var(--text)", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
                    {s.reason}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
