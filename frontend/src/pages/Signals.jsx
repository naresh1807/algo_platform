import { useEffect, useMemo, useState } from "react";
import { Search } from "lucide-react";

import EmptyState from "../components/EmptyState.jsx";
import ErrorState from "../components/ErrorState.jsx";
import LoadingSkeleton from "../components/LoadingSkeleton.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import { endpoints } from "../services/api.js";

function formatIst(isoString) {
  return new Date(isoString).toLocaleString("en-GB", {
    timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }) + " IST";
}

const SORTS = {
  newest: { label: "Newest first", cmp: (a, b) => new Date(b.created_at) - new Date(a.created_at) },
  oldest: { label: "Oldest first", cmp: (a, b) => new Date(a.created_at) - new Date(b.created_at) },
  score_desc: { label: "Score: high → low", cmp: (a, b) => (b.total_score ?? -1) - (a.total_score ?? -1) },
};

/**
 * Full signal history, including rejected ones with their `reason` text
 * -- this is the page the manual's "why a trade happened, why it was
 * rejected" requirement is really about (Dashboard.jsx only teases the
 * single most recent rejection). Kept as a card feed (not DataTable's
 * dense grid) since `reason` is free text that reads far better as a
 * paragraph than squeezed into a table cell -- spec explicitly allows
 * card presentation as the alternative to a table.
 */
export default function Signals() {
  const [signals, setSignals] = useState([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState("newest");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = () => {
    setLoading(true);
    setError(null);
    endpoints.signals(statusFilter ? { status: statusFilter } : {})
      .then((res) => setSignals(res.data.results ?? []))
      .catch(() => setError("Could not load signals."))
      .finally(() => setLoading(false));
  };

  useEffect(load, [statusFilter]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = q
      ? signals.filter((s) => s.symbol?.toLowerCase().includes(q) || s.reason?.toLowerCase().includes(q))
      : signals;
    return [...filtered].sort(SORTS[sortKey].cmp);
  }, [signals, query, sortKey]);

  return (
    <div className="panel">
      <h3>Signals</h3>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {["", "pending", "approved", "rejected", "executed", "expired"].map((s) => (
            <button
              key={s || "all"}
              type="button"
              className="btn btn-sm"
              onClick={() => setStatusFilter(s)}
              style={{
                background: statusFilter === s ? "var(--accent)" : "transparent",
                color: statusFilter === s ? "var(--accent-contrast)" : "var(--text)",
                borderColor: statusFilter === s ? "var(--accent)" : "var(--border)",
              }}
            >
              {s || "all"}
            </button>
          ))}
        </div>
        <div style={{ position: "relative", flex: "1 1 200px", minWidth: 180, maxWidth: 280 }}>
          <Search size={14} style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)", color: "var(--muted)" }} />
          <input
            className="input"
            placeholder="Search symbol or reason…"
            aria-label="Search symbol or reason"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ width: "100%", paddingLeft: 28 }}
          />
        </div>
        <select className="input" value={sortKey} onChange={(e) => setSortKey(e.target.value)}>
          {Object.entries(SORTS).map(([key, s]) => (
            <option key={key} value={key}>{s.label}</option>
          ))}
        </select>
      </div>

      {loading ? (
        <LoadingSkeleton rows={4} />
      ) : error ? (
        <ErrorState detail={error} onRetry={load} />
      ) : visible.length === 0 ? (
        <EmptyState title="No signals to show" />
      ) : (
        <div className="card-grid">
          {visible.map((s) => {
            const isBuy = s.signal_type === "buy";
            const isSell = s.signal_type === "sell";
            const accent = isBuy ? "green" : isSell ? "red" : "muted";
            const statusTone = s.status === "approved" || s.status === "executed" ? "ok" : s.status === "rejected" ? "bad" : "muted";
            return (
              <div key={s.id} className={`card card-accent-${accent}`}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 15, fontWeight: 700 }}>{s.symbol}</span>
                    <StatusBadge tone={isBuy ? "ok" : isSell ? "bad" : "muted"}>{s.signal_type}</StatusBadge>
                    {s.option_side && (
                      <StatusBadge tone="muted">
                        {s.strike_price ? `${s.strike_price} ` : ""}{s.option_side}
                      </StatusBadge>
                    )}
                  </div>
                  <StatusBadge tone={statusTone}>{s.status}</StatusBadge>
                </div>

                {s.created_at && (
                  <div style={{ marginTop: 4, fontSize: 11, color: "var(--muted)" }}>
                    {formatIst(s.created_at)}
                  </div>
                )}

                <div style={{ marginTop: 10, fontSize: 24, fontWeight: 700, color: isBuy ? "var(--green)" : isSell ? "var(--red)" : "var(--text)" }}>
                  {s.total_score?.toFixed?.(2) ?? "—"}
                </div>

                <div style={{ marginTop: 6, fontSize: 12, color: "var(--muted)", display: "flex", gap: 12, flexWrap: "wrap" }}>
                  <span>Options: {s.options_score?.toFixed?.(2) ?? "—"}</span>
                  <span>
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
