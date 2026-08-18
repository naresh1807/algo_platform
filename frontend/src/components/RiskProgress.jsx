/**
 * A single "current usage vs. configured limit" bar -- normal (green)
 * below `warnAt` (a fraction of `limit`, default 0.7), approaching-limit
 * (amber) up to `limit`, breached (red) at/above it. Never colors
 * without also labeling the state in text (accessibility requirement).
 * `limit` of null renders the bar as informational-only (no thresholds
 * to compare against -- real value, just nothing to measure it by yet).
 */
export default function RiskProgress({ label, value, limit, unit = "%", warnAt = 0.7, detail }) {
  const hasLimit = limit !== null && limit !== undefined && limit > 0;
  const pct = hasLimit ? Math.min(100, (Math.abs(value ?? 0) / limit) * 100) : 0;
  const ratio = hasLimit ? Math.abs(value ?? 0) / limit : 0;

  let tone = "ok";
  let toneColor = "var(--green)";
  let stateLabel = "Normal";
  if (hasLimit) {
    if (ratio >= 1) {
      tone = "bad"; toneColor = "var(--red)"; stateLabel = "Breached";
    } else if (ratio >= warnAt) {
      tone = "warn"; toneColor = "var(--amber)"; stateLabel = "Approaching limit";
    }
  }

  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{label}</span>
        <span style={{ fontSize: 12.5 }}>
          <strong style={{ color: hasLimit ? toneColor : "var(--text)" }}>
            {value === null || value === undefined ? "—" : `${Number(value).toFixed(1)}${unit}`}
          </strong>
          {hasLimit && <span style={{ color: "var(--muted)" }}> / {limit}{unit} limit</span>}
        </span>
      </div>
      <div style={{ height: 7, borderRadius: "var(--radius-pill)", background: "var(--elevated)", overflow: "hidden" }}>
        <div style={{ width: `${hasLimit ? pct : 0}%`, height: "100%", background: toneColor, borderRadius: "var(--radius-pill)" }} />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 3 }}>
        {detail ? <span style={{ fontSize: 11, color: "var(--muted)" }}>{detail}</span> : <span />}
        {hasLimit && (
          <span style={{ fontSize: 11, color: toneColor, fontWeight: 600 }}>{stateLabel}</span>
        )}
      </div>
    </div>
  );
}
