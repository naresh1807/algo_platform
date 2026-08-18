/**
 * lightweight-charts reads its own layout colors at chart-creation time
 * (and via applyOptions()) -- it has no idea about index.css's
 * --bg/--panel/--text CSS variables, so PriceChart/RSIPanel/MACDPanel
 * all need this explicit dark/light pair instead of just inheriting
 * the page theme automatically.
 */
// Kept in sync with index.css's [data-theme] token values (--card,
// --text, --border) -- lightweight-charts can't read CSS custom
// properties directly, so these are the same hexes duplicated here on
// purpose, not a second independent palette.
export const CHART_COLORS = {
  dark: {
    background: "#101827",
    text: "#f8fafc",
    grid: "#1f2a3d",
  },
  light: {
    background: "#ffffff",
    text: "#0f172a",
    grid: "#e2e8f0",
  },
};

export function chartLayoutOptions(theme) {
  const c = CHART_COLORS[theme] ?? CHART_COLORS.dark;
  return {
    layout: { background: { color: c.background }, textColor: c.text },
    grid: {
      vertLines: { color: c.grid },
      horzLines: { color: c.grid },
    },
  };
}

// Same --green/--red tokens as index.css, duplicated here for the same
// reason as CHART_COLORS above. Unlike the old palette, dark and light
// now use DIFFERENT green/red hexes (the new token set intentionally
// tunes each for its own background), so every series that colors by
// profit/loss/up/down must re-apply this on theme change, not just set
// it once at chart-creation time -- see the per-chart theme effect in
// PriceChart.jsx/RSIPanel.jsx/MACDPanel.jsx.
export const SEMANTIC_COLORS = {
  dark: { green: "#10b981", red: "#f43f5e" },
  light: { green: "#059669", red: "#dc2626" },
};

export function semanticColors(theme) {
  return SEMANTIC_COLORS[theme] ?? SEMANTIC_COLORS.dark;
}
