/**
 * lightweight-charts reads its own layout colors at chart-creation time
 * (and via applyOptions()) -- it has no idea about index.css's
 * --bg/--panel/--text CSS variables, so PriceChart/RSIPanel/MACDPanel
 * all need this explicit dark/light pair instead of just inheriting
 * the page theme automatically.
 */
export const CHART_COLORS = {
  dark: {
    background: "#131922",
    text: "#e6e9ef",
    grid: "#232b36",
  },
  light: {
    background: "#ffffff",
    text: "#14181f",
    grid: "#e3e7ed",
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
