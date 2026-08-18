/**
 * Small horizontal confidence gauge -- a percentage alone is easy to
 * skim past; a filled bar + the number together read faster, and the
 * fill color (not just position) carries the same green/amber/red
 * meaning as everywhere else so this never relies on color alone
 * (the numeric label is always shown too).
 */
export default function ConfidenceIndicator({ value, label = "Confidence" }) {
  if (value === null || value === undefined) return null;
  const pct = Math.max(0, Math.min(100, value));
  const tone = pct >= 65 ? "var(--green)" : pct >= 40 ? "var(--amber)" : "var(--red)";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 90 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--muted)" }}>
        <span>{label}</span>
        <span style={{ color: tone, fontWeight: 700 }}>{pct.toFixed(0)}%</span>
      </div>
      <div
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
        style={{ height: 6, borderRadius: "var(--radius-pill)", background: "var(--elevated)", overflow: "hidden" }}
      >
        <div style={{ width: `${pct}%`, height: "100%", background: tone, borderRadius: "var(--radius-pill)" }} />
      </div>
    </div>
  );
}
