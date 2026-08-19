import { createChart } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import { chartLayoutOptions, semanticColors } from "../constants/chartTheme.js";
import { istLocalizationOptions, istTimeScaleOptions } from "../constants/chartTime.js";

const toChartTime = (time) => Math.floor(new Date(time).getTime() / 1000);

const toLinePoints = (rows, key) =>
  rows.filter((r) => r[key] !== null && r[key] !== undefined).map((r) => ({ time: toChartTime(r.time ?? r.timestamp), value: Number(r[key]) }));

/**
 * One option contract's own candlestick chart: OHLC candles, a volume
 * histogram overlaid on the same pane (lightweight-charts v4 doesn't
 * support true multi-pane layouts -- volume shares the main chart's own
 * time axis via a second, bottom-margined price scale, the standard v4
 * convention; RSI/MACD stay as fully separate chart instances, see
 * RSIPanel.jsx/MACDPanel.jsx, which this component's caller renders
 * alongside it), and EMA9/EMA21/VWAP/Supertrend as overlay lines --
 * same create-once/update-in-place architecture as PriceChart.jsx:
 * `candles` only triggers a full setData() on a genuine contract/
 * timeframe switch, `liveCandle` updates the last bar in place via
 * series.update() on every tick, so a fast tick stream never rebuilds
 * the chart. `toggles` controls per-series visibility (applyOptions,
 * not add/remove) so switching an indicator on/off never recreates
 * anything either.
 */
export default function OptionCandleChart({
  candles = [],
  liveCandle = null,
  indicators = [],
  vwap = [],
  supertrend = [],
  theme = "dark",
  chartType = "candlestick",
  toggles = {},
  height = 380,
}) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const lineSeriesRef = useRef(null);
  const volumeSeriesRef = useRef(null);
  const ema9Ref = useRef(null);
  const ema21Ref = useRef(null);
  const vwapRef = useRef(null);
  const supertrendRef = useRef(null);
  const lastSeriesTimeRef = useRef(null);
  const [hoverBar, setHoverBar] = useState(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      ...chartLayoutOptions(theme),
      timeScale: istTimeScaleOptions(),
      localization: istLocalizationOptions,
      height,
    });
    const c = semanticColors(theme);

    const candleSeries = chart.addCandlestickSeries({
      upColor: c.green, downColor: c.red, borderVisible: false,
      wickUpColor: c.green, wickDownColor: c.red,
    });
    const lineSeries = chart.addLineSeries({ color: "#38bdf8", lineWidth: 2, visible: false });
    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" }, priceScaleId: "", color: c.green,
    });
    chart.priceScale("").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    const ema9 = chart.addLineSeries({ color: "#38bdf8", lineWidth: 1, title: "EMA9" });
    const ema21 = chart.addLineSeries({ color: "#f59e0b", lineWidth: 1, title: "EMA21" });
    const vwapSeries = chart.addLineSeries({ color: "#a78bfa", lineWidth: 1, title: "VWAP" });
    const supertrendSeries = chart.addLineSeries({ color: "#22d3ee", lineWidth: 1, title: "Supertrend" });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    lineSeriesRef.current = lineSeries;
    volumeSeriesRef.current = volumeSeries;
    ema9Ref.current = ema9;
    ema21Ref.current = ema21;
    vwapRef.current = vwapSeries;
    supertrendRef.current = supertrendSeries;

    chart.subscribeCrosshairMove((param) => {
      if (!param.time) {
        setHoverBar(null);
        return;
      }
      const bar = param.seriesData.get(candleSeries) ?? param.seriesData.get(lineSeries);
      const vol = param.seriesData.get(volumeSeries);
      if (!bar) {
        setHoverBar(null);
        return;
      }
      setHoverBar({
        open: bar.open, high: bar.high, low: bar.low, close: bar.close ?? bar.value,
        volume: vol?.value ?? null,
      });
    });

    const handleResize = () => chart.applyOptions({ width: containerRef.current.clientWidth });
    handleResize();
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Theme re-color, in place -- never rebuilds the chart.
  useEffect(() => {
    if (!chartRef.current) return;
    chartRef.current.applyOptions(chartLayoutOptions(theme));
    const c = semanticColors(theme);
    candleSeriesRef.current?.applyOptions({ upColor: c.green, downColor: c.red, wickUpColor: c.green, wickDownColor: c.red });
    volumeSeriesRef.current?.applyOptions({ color: c.green });
  }, [theme]);

  // Chart-type toggle: both series exist from creation, only visibility flips.
  useEffect(() => {
    candleSeriesRef.current?.applyOptions({ visible: chartType !== "line" });
    lineSeriesRef.current?.applyOptions({ visible: chartType === "line" });
  }, [chartType]);

  // Indicator toggles -- visibility only, never add/remove series.
  useEffect(() => {
    ema9Ref.current?.applyOptions({ visible: Boolean(toggles.ema9) });
    ema21Ref.current?.applyOptions({ visible: Boolean(toggles.ema21) });
    vwapRef.current?.applyOptions({ visible: Boolean(toggles.vwap) });
    supertrendRef.current?.applyOptions({ visible: Boolean(toggles.supertrend) });
    volumeSeriesRef.current?.applyOptions({ visible: Boolean(toggles.volume) });
  }, [toggles.ema9, toggles.ema21, toggles.vwap, toggles.supertrend, toggles.volume]);

  // Full history reset -- runs only when `candles` reference actually
  // changes (a real contract/timeframe switch, see useOptionCandles.js),
  // never on a live tick.
  useEffect(() => {
    if (!candleSeriesRef.current) return;

    const sorted = [...candles].sort((a, b) => new Date(a.time) - new Date(b.time));
    const deduped = sorted.filter((c, idx) => idx === 0 || toChartTime(c.time) !== toChartTime(sorted[idx - 1].time));

    const bars = deduped.map((c) => ({
      time: toChartTime(c.time), open: Number(c.open), high: Number(c.high), low: Number(c.low), close: Number(c.close),
    }));
    const c = semanticColors(theme);
    const volBars = deduped.map((c2) => ({
      time: toChartTime(c2.time), value: Number(c2.volume) || 0,
      color: Number(c2.close) >= Number(c2.open) ? c.green : c.red,
    }));
    const linePoints = deduped.map((c2) => ({ time: toChartTime(c2.time), value: Number(c2.close) }));

    candleSeriesRef.current.setData(bars);
    lineSeriesRef.current?.setData(linePoints);
    volumeSeriesRef.current?.setData(volBars);
    lastSeriesTimeRef.current = bars.length ? bars[bars.length - 1].time : null;
    chartRef.current?.timeScale().fitContent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candles]);

  // Live tick: cheap in-place update, guarded against an out-of-order
  // bar older than what's already drawn (same reasoning PriceChart.jsx
  // already documents for the underlying's own live candle).
  useEffect(() => {
    if (!candleSeriesRef.current || !liveCandle || lastSeriesTimeRef.current === null) return;
    const time = toChartTime(liveCandle.timestamp);
    if (time < lastSeriesTimeRef.current) return;

    const bar = { time, open: Number(liveCandle.open), high: Number(liveCandle.high), low: Number(liveCandle.low), close: Number(liveCandle.close) };
    candleSeriesRef.current.update(bar);
    lineSeriesRef.current?.update({ time, value: bar.close });
    const c = semanticColors(theme);
    volumeSeriesRef.current?.update({
      time, value: Number(liveCandle.volume) || 0, color: bar.close >= bar.open ? c.green : c.red,
    });
    lastSeriesTimeRef.current = time;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveCandle]);

  useEffect(() => {
    if (!ema9Ref.current) return;
    ema9Ref.current.setData(toLinePoints(indicators, "ema9"));
    ema21Ref.current.setData(toLinePoints(indicators, "ema21"));
  }, [indicators]);

  useEffect(() => {
    vwapRef.current?.setData(toLinePoints(vwap, "value"));
  }, [vwap]);

  useEffect(() => {
    supertrendRef.current?.setData(toLinePoints(supertrend, "value"));
  }, [supertrend]);

  return (
    <div style={{ position: "relative" }}>
      {hoverBar && (
        <div style={{ position: "absolute", top: 4, left: 8, zIndex: 2, fontSize: 11, color: "var(--muted)", display: "flex", gap: 10, pointerEvents: "none" }}>
          <span>O <strong style={{ color: "var(--text)" }}>{hoverBar.open?.toFixed(2)}</strong></span>
          <span>H <strong style={{ color: "var(--text)" }}>{hoverBar.high?.toFixed(2)}</strong></span>
          <span>L <strong style={{ color: "var(--text)" }}>{hoverBar.low?.toFixed(2)}</strong></span>
          <span>C <strong style={{ color: "var(--text)" }}>{hoverBar.close?.toFixed(2)}</strong></span>
          {hoverBar.volume != null && <span>Vol <strong style={{ color: "var(--text)" }}>{Number(hoverBar.volume).toLocaleString()}</strong></span>}
        </div>
      )}
      <div ref={containerRef} style={{ width: "100%" }} />
    </div>
  );
}
