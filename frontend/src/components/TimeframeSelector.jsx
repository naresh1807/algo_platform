/**
 * Pill-style timeframe picker (1m/5m/15m/1H/1D) -- reusable wherever a
 * chart needs one, replacing the bare <select> the Dashboard chart used
 * to have with something that reads as part of the chart's own
 * toolbar. `options` is the same {value,label} list constants/market.js
 * already exports (TIMEFRAMES), never redefined per caller.
 */
export default function TimeframeSelector({ options, value, onChange }) {
  return (
    <div role="group" aria-label="Chart timeframe" style={{ display: "inline-flex", gap: 2, background: "var(--elevated)", padding: 3, borderRadius: "var(--radius-sm)" }}>
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            aria-pressed={active}
            style={{
              border: "none",
              borderRadius: 6,
              padding: "5px 10px",
              fontSize: 12,
              fontWeight: 600,
              cursor: "pointer",
              background: active ? "var(--accent)" : "transparent",
              color: active ? "var(--accent-contrast)" : "var(--muted)",
            }}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
