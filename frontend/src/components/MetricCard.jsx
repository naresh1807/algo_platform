/**
 * A single Dashboard summary tile: title, primary value, optional
 * supporting sub-line, optional icon. Handles its own loading/empty
 * states so callers never render fabricated placeholder numbers --
 * pass `loading` while a fetch is in flight, or leave `value` null/
 * undefined for "not available yet" (renders "—", not a fake 0).
 *
 * `tone` colors the value: "positive"/"negative"/"warn" map to the
 * platform's green/red/amber tokens; omit for a neutral/default value.
 */
export default function MetricCard({ title, value, sub, icon: Icon, tone, loading = false }) {
  return (
    <div className="metric-card">
      <div className="metric-card-head">
        <span className="metric-card-title">{title}</span>
        {Icon && (
          <span className="metric-card-icon">
            <Icon size={15} />
          </span>
        )}
      </div>

      {loading ? (
        <div className="skeleton skeleton-line" style={{ width: "70%", height: 22 }} />
      ) : (
        <div className={`metric-card-value${tone ? ` ${tone}` : ""}`}>
          {value === null || value === undefined || value === "" ? "—" : value}
        </div>
      )}

      {sub && !loading && <div className="metric-card-sub">{sub}</div>}
    </div>
  );
}
