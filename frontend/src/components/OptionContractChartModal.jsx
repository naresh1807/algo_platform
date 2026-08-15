import { useEffect, useState } from "react";

import OptionPriceChart from "../charts/OptionPriceChart.jsx";
import { endpoints } from "../services/api.js";
import { useThemeStore } from "../store/themeStore.js";

/**
 * Broker-app-parity popup (manual's "click a strike to see its own
 * chart" convention, matching what Kite/Angel One do) -- opened from
 * OptionsAnalytics.jsx's chain table by clicking a strike's CE/PE LTP
 * cell. `contract` is {strike, optionType, underlying, expiry} | null;
 * rendering nothing when null is what makes this a no-op to mount
 * once in the page rather than every row needing its own modal
 * instance.
 */
export default function OptionContractChartModal({ contract, onClose }) {
  const [snapshots, setSnapshots] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const theme = useThemeStore((s) => s.theme);

  useEffect(() => {
    if (!contract) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    endpoints
      .optionContractHistory(contract.underlying, contract.expiry, contract.strike, contract.optionType)
      .then((res) => {
        if (cancelled) return;
        setSnapshots(res.data.results ?? []);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setError("Could not load this contract's price history.");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [contract]);

  if (!contract) return null;

  const latest = snapshots[0]; // API returns newest-first

  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "rgba(0, 0, 0, 0.55)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        className="panel"
        style={{ width: "min(720px, 92vw)", margin: 0 }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>
            {contract.underlying} {contract.strike} {contract.optionType}
            <span style={{ fontSize: 12, fontWeight: 400, color: "var(--muted)", marginLeft: 8 }}>
              exp {contract.expiry}
            </span>
          </h3>
          <button
            onClick={onClose}
            style={{ background: "none", border: "none", color: "var(--muted)", fontSize: 18, cursor: "pointer", lineHeight: 1 }}
          >
            ×
          </button>
        </div>

        {latest && (
          <div style={{ display: "flex", gap: 16, margin: "8px 0 4px", fontSize: 13 }}>
            <span>LTP <strong>{latest.ltp}</strong></span>
            <span style={{ color: "var(--muted)" }}>OI {Number(latest.open_interest).toLocaleString()}</span>
            <span style={{ color: "var(--muted)" }}>Vol {Number(latest.volume).toLocaleString()}</span>
            {latest.iv != null && <span style={{ color: "var(--muted)" }}>IV {latest.iv}%</span>}
          </div>
        )}

        {loading && <p style={{ color: "var(--muted)", marginTop: 12 }}>Loading…</p>}
        {error && <p style={{ color: "var(--red)", marginTop: 12 }}>{error}</p>}
        {!loading && !error && snapshots.length === 0 && (
          <p style={{ color: "var(--muted)", marginTop: 12 }}>
            No snapshot history yet for this contract -- check back after the next ingestion cycle.
          </p>
        )}
        {!loading && !error && snapshots.length > 0 && (
          <div style={{ marginTop: 8 }}>
            <OptionPriceChart snapshots={snapshots} theme={theme} />
          </div>
        )}
      </div>
    </div>
  );
}
