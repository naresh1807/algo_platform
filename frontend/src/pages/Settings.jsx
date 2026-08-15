import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";

const CONFIRM_PHRASE = "ENABLE LIVE TRADING";

/**
 * Paper/Live trading toggle -- apps.execution.views.ExecutionModeView.
 * Switching to LIVE means apps.execution.tasks.run_trading_cycle
 * starts placing real orders with real money on its very next
 * scheduled run, so that direction requires typing CONFIRM_PHRASE
 * exactly (enforced again server-side, this is a UX guard not the
 * real one). Switching back to PAPER is always one click, no
 * confirmation -- de-risking should never have friction, only making
 * things riskier should.
 */
export default function Settings() {
  const [current, setCurrent] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [confirmText, setConfirmText] = useState("");
  const [showConfirm, setShowConfirm] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = () => {
    setLoading(true);
    endpoints
      .executionMode()
      .then((res) => {
        setCurrent(res.data);
        setLoading(false);
      })
      .catch(() => {
        setError("Could not load the current execution mode.");
        setLoading(false);
      });
  };

  useEffect(load, []);

  const applyMode = (mode, phrase) => {
    setSaving(true);
    setError(null);
    endpoints
      .setExecutionMode(mode, phrase)
      .then((res) => {
        setCurrent(res.data);
        setSaving(false);
        setShowConfirm(false);
        setConfirmText("");
      })
      .catch((err) => {
        setError(err.response?.data?.error || "Could not change execution mode.");
        setSaving(false);
      });
  };

  const isLive = current?.mode === "live";

  return (
    <div>
      <div className="panel">
        <h3>Trading Mode</h3>
        <p style={{ fontSize: 13, color: "var(--muted)", marginTop: -4 }}>
          Controls apps.execution.tasks.run_trading_cycle -- Paper simulates every fill against
          real market data with zero real-money risk; Live places real orders through your
          connected broker.
        </p>

        {loading && <p style={{ color: "var(--muted)" }}>Loading…</p>}

        {!loading && current && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "12px 0" }}>
              <span className={`status-dot ${isLive ? "bad" : "ok"}`} />
              <span style={{ fontSize: 18, fontWeight: 700, textTransform: "uppercase" }}>
                {current.mode}
              </span>
              {current.changed_by && (
                <span style={{ fontSize: 12, color: "var(--muted)" }}>
                  — last changed by {current.changed_by} at{" "}
                  {new Date(current.changed_at).toLocaleString()}
                </span>
              )}
            </div>

            {isLive && (
              <p style={{ color: "var(--red)", fontSize: 13, fontWeight: 600 }}>
                ⚠ Live trading is active. Real orders will be placed with real money on the
                next scheduled trading cycle.
              </p>
            )}

            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button
                disabled={saving || current.mode === "paper"}
                onClick={() => applyMode("paper")}
                style={{
                  padding: "8px 16px", borderRadius: 6, cursor: "pointer",
                  border: "1px solid var(--border)",
                  background: current.mode === "paper" ? "var(--accent)" : "transparent",
                  color: current.mode === "paper" ? "#fff" : "var(--text)",
                  fontWeight: 600,
                }}
              >
                Paper Mode
              </button>
              <button
                disabled={saving || current.mode === "live"}
                onClick={() => setShowConfirm(true)}
                style={{
                  padding: "8px 16px", borderRadius: 6, cursor: "pointer",
                  border: "1px solid var(--red)",
                  background: current.mode === "live" ? "var(--red)" : "transparent",
                  color: current.mode === "live" ? "#fff" : "var(--red)",
                  fontWeight: 600,
                }}
              >
                Live Mode
              </button>
            </div>

            {showConfirm && current.mode !== "live" && (
              <div
                style={{
                  marginTop: 16, padding: 12, borderRadius: 8,
                  border: "1px solid var(--red)", background: "color-mix(in srgb, var(--red) 8%, transparent)",
                }}
              >
                <p style={{ margin: "0 0 8px", fontSize: 13 }}>
                  This enables real order placement with real money. Type{" "}
                  <strong>{CONFIRM_PHRASE}</strong> to confirm:
                </p>
                <div style={{ display: "flex", gap: 8 }}>
                  <input
                    value={confirmText}
                    onChange={(e) => setConfirmText(e.target.value)}
                    placeholder={CONFIRM_PHRASE}
                    style={{
                      flex: 1, padding: 8, borderRadius: 6, border: "1px solid var(--border)",
                      background: "var(--bg)", color: "var(--text)",
                    }}
                  />
                  <button
                    disabled={saving || confirmText !== CONFIRM_PHRASE}
                    onClick={() => applyMode("live", confirmText)}
                    style={{
                      padding: "8px 16px", borderRadius: 6, border: "none",
                      background: confirmText === CONFIRM_PHRASE ? "var(--red)" : "var(--border)",
                      color: "#fff", cursor: confirmText === CONFIRM_PHRASE ? "pointer" : "default",
                      fontWeight: 600,
                    }}
                  >
                    Confirm
                  </button>
                  <button
                    onClick={() => {
                      setShowConfirm(false);
                      setConfirmText("");
                    }}
                    style={{
                      padding: "8px 16px", borderRadius: 6, cursor: "pointer",
                      border: "1px solid var(--border)", background: "transparent", color: "var(--text)",
                    }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </>
        )}

        {error && <p style={{ color: "var(--red)", marginTop: 12 }}>{error}</p>}
      </div>
    </div>
  );
}
