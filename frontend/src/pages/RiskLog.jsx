import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";

/**
 * This is the audit-trail view over apps.risk.RiskEvent -- every block,
 * size-reduction, or kill-switch trip the risk engine has ever logged.
 * Deliberately no "clear kill switch" button anywhere in this UI: manual
 * section 21 treats re-arming as a deliberate backend-only action, not
 * something exposed to a stray click in the dashboard.
 */
export default function RiskLog() {
  const [events, setEvents] = useState([]);
  const [killSwitch, setKillSwitch] = useState(null);
  const [equity, setEquity] = useState(null);
  const [severityFilter, setSeverityFilter] = useState("");

  // The `risk_live` WebSocket group (same one TopBar/Dashboard already
  // read) pushes kill-switch flips the instant they happen. Previously
  // this page only ever fetched a one-time REST snapshot on mount, so
  // it could show a stale "Inactive" here even after TopBar had already
  // flipped to showing the kill switch tripped elsewhere on the page.
  const liveKillSwitchActive = useLiveStore((s) => s.killSwitchActive);

  useEffect(() => {
    endpoints.killSwitch().then((res) => setKillSwitch(res.data));
    endpoints.equity().then((res) => setEquity(res.data));
  }, []);

  // Overlay live pushes onto the REST snapshot once it's loaded. Guarded
  // on `prev` already being set so the socket's default `false` value
  // (before any message has actually arrived) can never clobber a
  // REST-fetched `is_active: true` before the first real push -- this
  // only ever applies a value that came from an actual live message.
  useEffect(() => {
    setKillSwitch((prev) => (prev ? { ...prev, is_active: liveKillSwitchActive } : prev));
  }, [liveKillSwitchActive]);

  useEffect(() => {
    endpoints.riskEvents(severityFilter ? { severity: severityFilter } : {}).then((res) => {
      setEvents(res.data.results ?? []);
    });
  }, [severityFilter]);

  return (
    <>
      <div className="panel">
        <h3>Kill Switch State</h3>
        {killSwitch ? (
          <p>
            <span className={`status-dot ${killSwitch.is_active ? "bad" : "ok"}`} />
            {killSwitch.is_active ? "ACTIVE" : "Inactive"}
            {killSwitch.activated_at && ` — activated ${new Date(killSwitch.activated_at).toLocaleString()}`}
          </p>
        ) : (
          <p style={{ color: "var(--muted)" }}>Loading…</p>
        )}
      </div>

      <div className="panel">
        <h3>Equity / Drawdown</h3>
        {equity ? (
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            <li>Current equity: {equity.current_equity}</li>
            <li>Peak equity: {equity.peak_equity}</li>
            <li>Drawdown: {equity.drawdown_pct?.toFixed?.(1)}%</li>
            <li>Today's P&amp;L: {equity.daily_pnl_pct?.toFixed?.(1)}%</li>
            <li>Consecutive losses: {equity.consecutive_losses}</li>
          </ul>
        ) : (
          <p style={{ color: "var(--muted)" }}>Loading…</p>
        )}
      </div>

      <div className="panel">
        <h3>Risk Event Log</h3>
        <div style={{ marginBottom: 12 }}>
          {["", "info", "warning", "critical"].map((s) => (
            <button
              key={s || "all"}
              onClick={() => setSeverityFilter(s)}
              style={{
                marginRight: 6,
                padding: "4px 10px",
                borderRadius: 6,
                border: "1px solid var(--border)",
                background: severityFilter === s ? "var(--accent)" : "transparent",
                color: severityFilter === s ? "#fff" : "var(--text)",
                cursor: "pointer",
              }}
            >
              {s || "all"}
            </button>
          ))}
        </div>

        {events.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No risk events logged.</p>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                <th>Severity</th><th>Event</th><th>Symbol</th><th>Message</th><th>When</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td>{e.severity}</td>
                  <td>{e.event_type}</td>
                  <td>{e.symbol || "—"}</td>
                  <td>{e.message}</td>
                  <td>{new Date(e.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
