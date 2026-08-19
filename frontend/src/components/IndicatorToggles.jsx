const INDICATOR_LABELS = [
  { key: "ema9", label: "EMA 9" },
  { key: "ema21", label: "EMA 21" },
  { key: "vwap", label: "VWAP" },
  { key: "supertrend", label: "Supertrend" },
  { key: "rsi", label: "RSI 14" },
  { key: "macd", label: "MACD" },
  { key: "volume", label: "Volume" },
];

/**
 * Small pill-toggle row for an option contract chart's indicator set.
 * Purely controlled -- `toggles`/`onToggle` are owned by the caller
 * (OptionContractChartModal.jsx), which is also what decides the
 * defaults (EMA9/EMA21/VWAP/Volume enabled per the manual's spec).
 */
export default function IndicatorToggles({ toggles, onToggle }) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
      {INDICATOR_LABELS.map(({ key, label }) => {
        const active = Boolean(toggles[key]);
        return (
          <button
            key={key}
            type="button"
            onClick={() => onToggle(key)}
            aria-pressed={active}
            style={{
              border: "1px solid var(--border)",
              borderRadius: "var(--radius-pill)",
              padding: "3px 10px",
              fontSize: 11,
              fontWeight: 600,
              cursor: "pointer",
              background: active ? "var(--accent)" : "transparent",
              color: active ? "var(--accent-contrast)" : "var(--muted)",
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
