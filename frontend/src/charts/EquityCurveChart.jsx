import { createChart } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { chartLayoutOptions } from "../constants/chartTheme.js";
import { istLocalizationOptions, istTimeScaleOptions } from "../constants/chartTime.js";

const toChartTime = (timestamp) => Math.floor(new Date(timestamp).getTime() / 1000);

/**
 * apps.risk.EquitySnapshot's raw account-equity time series
 * (apps.risk.views.EquityCurveView) -- a line series, same lightweight-
 * charts setup as OptionPriceChart.jsx (that component's own docstring
 * explains why: point-in-time readings, not OHLC bars, so a line series
 * is the honest representation, not candles).
 */
export default function EquityCurveChart({ points = [], theme = "dark" }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      ...chartLayoutOptions(theme),
      timeScale: istTimeScaleOptions(),
      localization: istLocalizationOptions,
      height: 260,
    });
    const series = chart.addLineSeries({ color: "#34d399", lineWidth: 2 });

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
    // API already returns ascending-by-time (EquityCurveView orders by
    // timestamp), but dedupe same-second points defensively, same
    // reasoning as OptionPriceChart.jsx.
    const sorted = [...points].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    const data = sorted
      .filter((p, idx) => idx === 0 || toChartTime(p.timestamp) !== toChartTime(sorted[idx - 1].timestamp))
      .map((p) => ({ time: toChartTime(p.timestamp), value: Number(p.equity) }));
    seriesRef.current.setData(data);
    chartRef.current?.timeScale().fitContent();
  }, [points]);

  return <div ref={containerRef} style={{ width: "100%" }} />;
}
