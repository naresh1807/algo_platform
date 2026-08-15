import { createChart } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { chartLayoutOptions } from "../constants/chartTheme.js";
import { istLocalizationOptions, istTimeScaleOptions } from "../constants/chartTime.js";

const toChartTime = (timestamp) => Math.floor(new Date(timestamp).getTime() / 1000);

/**
 * One option contract's own LTP over time -- a line series, not
 * candles, since OptionChainSnapshot stores point-in-time premium
 * readings from each ~5-minute ingestion poll (apps/options/tasks.py's
 * ingest_option_chain_snapshots), not real OHLC bars the way
 * HistoricalData does for the underlying. Same lightweight-charts
 * setup as PriceChart.jsx (theme/time-axis helpers) so this reads as
 * the same charting system, not a visually different one-off.
 */
export default function OptionPriceChart({ snapshots = [], theme = "dark" }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      ...chartLayoutOptions(theme),
      timeScale: istTimeScaleOptions(),
      localization: istLocalizationOptions,
      height: 320,
    });
    const series = chart.addLineSeries({ color: "#38bdf8", lineWidth: 2 });

    chartRef.current = chart;
    seriesRef.current = series;

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
    if (!seriesRef.current) return;
    // API returns newest-first (OptionChainSnapshot.Meta.ordering);
    // setData() requires strictly ascending time, same reason
    // PriceChart.jsx sorts/dedupes its own candles before drawing.
    const sorted = [...snapshots].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    const points = sorted
      .filter((s, idx) => idx === 0 || toChartTime(s.timestamp) !== toChartTime(sorted[idx - 1].timestamp))
      .map((s) => ({ time: toChartTime(s.timestamp), value: Number(s.ltp) }));
    seriesRef.current.setData(points);
    chartRef.current?.timeScale().fitContent();
  }, [snapshots]);

  return <div ref={containerRef} style={{ width: "100%" }} />;
}
