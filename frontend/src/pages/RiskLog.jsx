import { useEffect, useState } from "react";
import { ShieldAlert, ShieldCheck } from "lucide-react";

import DataTable from "../components/DataTable.jsx";
import RiskProgress from "../components/RiskProgress.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";

// Mirrors config/settings.py's current RISK_HARD_LIMITS values -- same
// pattern components/DecisionFactors.jsx's own REFERENCE_MAX_SPREAD_PCT
// already established for this exact situation (a real backend
// constant with no REST field exposing it yet). These are deploy-time
// config, not runtime-editable (see that settings dict's own docstring:
// "never DB-editable"), so the staleness risk of mirroring them here is
// low, but if an operator changes RISK_HARD_LIMITS in settings.py this
// reference needs updating too -- it is NOT fetched live.
const REFERENCE_LIMITS = {
  MAX_DAILY_LOSS_PCT: 3.0,
  DRAWDOWN_PAUSE_PCT: 15.0,
  DRAWDOWN_FLATTEN_PCT: 20.0,
  MAX_CONSECUTIVE_LOSSES: 3,
  MAX_OPEN_POSITIONS: 5,
  MAX_OPEN_RISK_PCT: 6.0,
};

/**
 * This is the audit-trail view over apps.risk.RiskEvent -- every block,
 * size-reduction, or kill-switch trip the risk engine has ever logged.
 * Deliberately no "clear kill switch" button anywhere in this UI: manual
 * section 21 treats re-arming as a deliberate backend-only action, not
 * something exposed to a stray click in the dashboard.
 */
export default function RiskLog() {
  const [events, setEvents] = useState([]);
  const [eventsLoading, setEventsLoading] = useState(true);
  const [killSwitch, setKillSwitch] = useState(null);
  const [equity, setEquity] = useState(null);
  const [positions, setPositions] = useState([]);
  const [severityFilter, setSeverityFilter] = useState("");

  const liveKillSwitchActive = useLiveStore((s) => s.killSwitchActive);

  useEffect(() => {
    endpoints.killSwitch().then((res) => setKillSwitch(res.data));
    endpoints.equity().then((res) => setEquity(res.data));
    endpoints.positions().then((res) => setPositions(res.data.results ?? []));
  }, []);

  useEffect(() => {
    setKillSwitch((prev) => (prev ? { ...prev, is_active: liveKillSwitchActive } : prev));
  }, [liveKillSwitchActive]);

  const loadEvents = () => {
    setEventsLoading(true);
    endpoints.riskEvents(severityFilter ? { severity: severityFilter } : {}).then((res) => {
      setEvents(res.data.results ?? []);
      setEventsLoading(false);
    });
  };
  useEffect(loadEvents, [severityFilter]);

  const capitalDeployed = positions.reduce((sum, p) => sum + Number(p.entry_price ?? 0) * Number(p.qty ?? 0), 0);
  const exposurePct = equity?.current_equity ? (capitalDeployed / Number(equity.current_equity)) * 100 : null;
  const dailyLossPct = equity?.daily_pnl_pct != null && equity.daily_pnl_pct < 0 ? Math.abs(equity.daily_pnl_pct) : 0;

  const eventColumns = [
    {
      key: "severity", label: "Severity",
      render: (e) => <StatusBadge tone={e.severity === "critical" ? "bad" : e.severity === "warning" ? "warn" : "muted"}>{e.severity}</StatusBadge>,
    },
    { key: "event_type", label: "Event" },
    { key: "symbol", label: "Symbol", render: (e) => e.symbol || "—" },
    { key: "message", label: "Message" },
    { key: "created_at", label: "When", render: (e) => new Date(e.created_at).toLocaleString() },
  ];

  return (
    <>
      <div className="panel">
        <h3>Kill Switch State</h3>
        {killSwitch ? (
          <StatusBadge tone={killSwitch.is_active ? "bad" : "ok"} icon={killSwitch.is_active ? ShieldAlert : ShieldCheck}>
            {killSwitch.is_active ? "ACTIVE" : "Inactive"}
            {killSwitch.activated_at && ` — activated ${new Date(killSwitch.activated_at).toLocaleString()}`}
          </StatusBadge>
        ) : (
          <p style={{ color: "var(--muted)" }}>Loading…</p>
        )}
      </div>

      <div className="panel">
        <h3>Risk Utilization</h3>
        {equity ? (
          <>
            <RiskProgress
              label="Daily Loss"
              value={dailyLossPct}
              limit={REFERENCE_LIMITS.MAX_DAILY_LOSS_PCT}
              detail="% of the day's starting equity"
            />
            <RiskProgress
              label="Drawdown"
              value={equity.drawdown_pct}
              limit={REFERENCE_LIMITS.DRAWDOWN_FLATTEN_PCT}
              warnAt={REFERENCE_LIMITS.DRAWDOWN_PAUSE_PCT / REFERENCE_LIMITS.DRAWDOWN_FLATTEN_PCT}
              detail={`Pauses new entries at ${REFERENCE_LIMITS.DRAWDOWN_PAUSE_PCT}%, flattens everything at ${REFERENCE_LIMITS.DRAWDOWN_FLATTEN_PCT}%`}
            />
            <RiskProgress
              label="Exposure"
              value={exposurePct}
              limit={REFERENCE_LIMITS.MAX_OPEN_RISK_PCT}
              detail="Capital deployed as % of available balance"
            />
            <RiskProgress
              label="Consecutive Losses"
              value={equity.consecutive_losses}
              limit={REFERENCE_LIMITS.MAX_CONSECUTIVE_LOSSES}
              unit=""
              detail="Cooldown triggers at the limit"
            />
            <RiskProgress
              label="Open Positions"
              value={positions.length}
              limit={REFERENCE_LIMITS.MAX_OPEN_POSITIONS}
              unit=""
            />
          </>
        ) : (
          <p style={{ color: "var(--muted)" }}>Loading…</p>
        )}
      </div>

      <div className="panel">
        <h3>Equity</h3>
        {equity ? (
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            <li>Current equity: {equity.current_equity}</li>
            <li>Peak equity: {equity.peak_equity}</li>
            <li>Today's P&amp;L: {equity.daily_pnl_pct?.toFixed?.(1)}%</li>
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
              type="button"
              className="btn btn-sm"
              onClick={() => setSeverityFilter(s)}
              style={{
                marginRight: 6,
                background: severityFilter === s ? "var(--accent)" : "transparent",
                color: severityFilter === s ? "var(--accent-contrast)" : "var(--text)",
                borderColor: severityFilter === s ? "var(--accent)" : "var(--border)",
              }}
            >
              {s || "all"}
            </button>
          ))}
        </div>

        <DataTable
          columns={eventColumns}
          rows={events}
          getRowKey={(e) => e.id}
          searchKeys={["event_type", "symbol", "message"]}
          searchPlaceholder="Search risk events…"
          loading={eventsLoading}
          onRetry={loadEvents}
          emptyTitle="No risk events logged"
        />
      </div>
    </>
  );
}
