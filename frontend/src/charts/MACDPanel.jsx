import { createChart } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { chartLayoutOptions, semanticColors } from "../constants/chartTheme.js";
import { istLocalizationOptions, istTimeScaleOptions } from "../constants/chartTime.js";

const toChartTime = (timestamp) => Math.floor(new Date(timestamp).getTime() / 1000);

/**
 * Standalone MACD(12,26,9) pane below the price/RSI charts -- MACD +
 * signal as lines, histogram as the usual green/red bars around zero,
 * matching how MACD is drawn on every mainstream charting platform.
 * Separate chart instance for the same lightweight-charts v4
 * single-pane-per-chart reason documented in RSIPanel.jsx. `theme`
 * re-colors the chart canvas the same way PriceChart/RSIPanel do.
 */
export default function MACDPanel({ indicators = [], theme = "dark" }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const macdRef = useRef(null);
  const signalRef = useRef(null);
  const histRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      ...chartLayoutOptions(theme),
      timeScale: istTimeScaleOptions(),
      localization: istLocalizationOptions,
      height: 140,
    });
    const hist = chart.addHistogramSeries({ title: "MACD Hist" });
    const macd = chart.addLineSeries({ color: "#38bdf8", lineWidth: 1, title: "MACD" });
    const signal = chart.addLineSeries({ color: "#f59e0b", lineWidth: 1, title: "Signal" });

    chartRef.current = chart;
    macdRef.current = macd;
    signalRef.current = signal;
    histRef.current = hist;

    const handleResize = () =>
      chart.applyOptions({ width: containerRef.current.clientWidth });
    handleResize();
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, []);

  useEffect(() => {
    if (!chartRef.current) return;
    chartRef.current.applyOptions(chartLayoutOptions(theme));
  }, [theme]);

  useEffect(() => {
    if (!macdRef.current) return;
    const sorted = [...indicators]
      .filter((r) => r.macd !== null && r.macd !== undefined)
      .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
    const c = semanticColors(theme);

    macdRef.current.setData(
      sorted.map((r) => ({ time: toChartTime(r.timestamp), value: Number(r.macd) }))
    );
    signalRef.current.setData(
      sorted.map((r) => ({ time: toChartTime(r.timestamp), value: Number(r.macd_signal) }))
    );
    histRef.current.setData(
      sorted.map((r) => ({
        time: toChartTime(r.timestamp),
        value: Number(r.macd_hist),
        color: Number(r.macd_hist) >= 0 ? c.green : c.red,
      }))
    );
    // Re-runs on theme change too (not just indicators) so the
    // histogram bars' colors stay in sync with the active theme's
    // green/red tokens, same as PriceChart's candles/RSIPanel's
    // reference lines.
  }, [indicators, theme]);

  return <div ref={containerRef} style={{ width: "100%" }} />;
}
