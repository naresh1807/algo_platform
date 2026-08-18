import { createChart } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { chartLayoutOptions, semanticColors } from "../constants/chartTheme.js";
import { istLocalizationOptions, istTimeScaleOptions } from "../constants/chartTime.js";

const toChartTime = (timestamp) => Math.floor(new Date(timestamp).getTime() / 1000);

/**
 * Standalone RSI(14) pane below the price chart -- kept as its own
 * small chart instance (rather than a second pane on PriceChart's
 * chart) because lightweight-charts v4 doesn't support multi-pane
 * layouts; a separate synced-width chart under the price chart is the
 * standard v4 way to render an oscillator panel. Draws the 70/30
 * overbought/oversold reference lines every trading platform shows
 * alongside RSI, since a bare 0-100 line without them is much harder
 * to read at a glance. `theme` re-colors the chart canvas the same way
 * PriceChart does -- see chartTheme.js.
 */
export default function RSIPanel({ indicators = [], theme = "dark" }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const rsiRef = useRef(null);
  const overboughtLineRef = useRef(null);
  const oversoldLineRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      ...chartLayoutOptions(theme),
      timeScale: istTimeScaleOptions(),
      localization: istLocalizationOptions,
      rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.1 } },
      height: 140,
    });
    const initialColors = semanticColors(theme);
    const rsi = chart.addLineSeries({ color: "#38bdf8", lineWidth: 1, title: "RSI 14" });
    overboughtLineRef.current = rsi.createPriceLine({ price: 70, color: initialColors.red, lineStyle: 2, lineWidth: 1, title: "70" });
    oversoldLineRef.current = rsi.createPriceLine({ price: 30, color: initialColors.green, lineStyle: 2, lineWidth: 1, title: "30" });

    chartRef.current = chart;
    rsiRef.current = rsi;

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
    const c = semanticColors(theme);
    overboughtLineRef.current?.applyOptions({ color: c.red });
    oversoldLineRef.current?.applyOptions({ color: c.green });
  }, [theme]);

  useEffect(() => {
    if (!rsiRef.current) return;
    const sorted = [...indicators]
      .filter((r) => r.rsi !== null && r.rsi !== undefined)
      .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
    rsiRef.current.setData(
      sorted.map((r) => ({ time: toChartTime(r.timestamp), value: Number(r.rsi) }))
    );
  }, [indicators]);

  return <div ref={containerRef} style={{ width: "100%" }} />;
}
