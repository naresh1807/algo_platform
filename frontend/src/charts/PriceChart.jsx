import { createChart, LineStyle } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { chartLayoutOptions } from "../constants/chartTheme.js";
import { istLocalizationOptions, istTimeScaleOptions } from "../constants/chartTime.js";

// Shared by every series built off `timestamp` fields (candles AND the
// indicators/ series) -- both must be converted to lightweight-charts'
// integer-seconds `time` the same way, or the EMA/SAR overlays would
// drift a candle or two off from the price bars they're meant to track.
const toChartTime = (timestamp) => Math.floor(new Date(timestamp).getTime() / 1000);

// Drops any point whose value is null (a real gap, e.g. EMA before it
// has enough lookback) -- lightweight-charts throws on a null `value`,
// so these have to be filtered out rather than passed through.
const toLinePoints = (rows, key) =>
  rows
    .filter((r) => r[key] !== null && r[key] !== undefined)
    .map((r) => ({ time: toChartTime(r.timestamp), value: Number(r[key]) }));

/**
 * Thin wrapper around TradingView's lightweight-charts (manual section
 * 4). Renders candles plus, when `indicators` is supplied, EMA9/EMA21
 * (trend-following overlay lines) and Parabolic SAR (dotted overlay) on
 * the same price pane -- same convention as Angel One/most charting
 * platforms, where these three sit directly on price rather than in a
 * separate panel. It still does not fetch data or compute indicators
 * itself; `indicators` is expected to come from
 * endpoints.indicators(symbol, timeframe) already computed server-side.
 *
 * `theme` ("dark"/"light") re-colors the chart's own background/grid/
 * text -- these are lightweight-charts canvas options, not CSS, so they
 * can't just inherit index.css's --bg/--text variables like the rest of
 * the page does.
 */
export default function PriceChart({ candles = [], liveCandle = null, indicators = [], theme = "dark" }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const ema9Ref = useRef(null);
  const ema21Ref = useRef(null);
  const sarRef = useRef(null);
  // Tracks the newest bar time already in the series (set by the
  // candles/setData effect below) -- update() below refuses to apply a
  // liveCandle older than this, which can otherwise happen for one
  // render right after a symbol/timeframe switch, before the new
  // history has finished loading but a stale liveCandle from the OLD
  // selection is still in flight.
  const lastSeriesTimeRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      ...chartLayoutOptions(theme),
      timeScale: istTimeScaleOptions(),
      localization: istLocalizationOptions,
      height: 420,
    });
    const series = chart.addCandlestickSeries({
      upColor: "#16a34a",
      downColor: "#dc2626",
      borderVisible: false,
      wickUpColor: "#16a34a",
      wickDownColor: "#dc2626",
    });

    // EMA9/EMA21: the manual's own trend-following pair (section 11),
    // so these are the two lines a trader would expect to see over the
    // candles first. SAR renders as small dots (its usual charting
    // convention -- it's a trailing stop level, not something you'd
    // read as a continuous line) via a thin dotted line rather than a
    // solid one.
    const ema9 = chart.addLineSeries({ color: "#38bdf8", lineWidth: 1, title: "EMA9" });
    const ema21 = chart.addLineSeries({ color: "#f59e0b", lineWidth: 1, title: "EMA21" });
    const sar = chart.addLineSeries({
      color: "#a78bfa",
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: "SAR",
    });

    chartRef.current = chart;
    seriesRef.current = series;
    ema9Ref.current = ema9;
    ema21Ref.current = ema21;
    sarRef.current = sar;

    const handleResize = () =>
      chart.applyOptions({ width: containerRef.current.clientWidth });
    handleResize();
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, []);

  // Re-colors the existing chart instance in place on toggle, rather
  // than tearing it down and rebuilding it -- avoids losing the user's
  // current zoom/pan position just because they switched themes.
  useEffect(() => {
    if (!chartRef.current) return;
    chartRef.current.applyOptions(chartLayoutOptions(theme));
  }, [theme]);

  useEffect(() => {
    if (!seriesRef.current) return;

    // HistoricalData.Meta.ordering is "-timestamp" (newest-first --
    // correct for the backend's own "give me the latest N candles"
    // queries), but lightweight-charts' setData() requires strictly
    // ASCENDING time order and will reject/silently show nothing
    // otherwise. Sort and dedupe by numeric candle time so timezone-
    // formatted live updates and REST-fetched history can't leave
    // duplicate points in the series.
    const sorted = [...candles].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    const deduped = sorted.filter((c, idx) =>
      idx === 0 || toChartTime(c.timestamp) !== toChartTime(sorted[idx - 1].timestamp)
    );

    const points = deduped.map((c) => ({
      time: toChartTime(c.timestamp),
      open: Number(c.open),
      high: Number(c.high),
      low: Number(c.low),
      close: Number(c.close),
    }));
    seriesRef.current.setData(points);
    lastSeriesTimeRef.current = points.length ? points[points.length - 1].time : null;
  }, [candles]);

  // Live tick updates: incremental series.update() (lightweight-charts'
  // own API for exactly this -- redraws only the last bar) instead of
  // re-running the full setData() above per tick, which would rebuild
  // the entire series on every message once ticks arrive multiple times
  // a second (apps/market_data/tick_aggregator.py's live feed). The
  // time-ordering guard rejects a liveCandle older than the newest bar
  // already drawn -- update() throws on out-of-order input, and that can
  // otherwise happen for one render right after a symbol/timeframe
  // switch (see the `liveCandle` prop's own caller in Dashboard.jsx,
  // which additionally withholds it while the new history is loading).
  useEffect(() => {
    if (!seriesRef.current || !liveCandle || lastSeriesTimeRef.current === null) return;
    const time = toChartTime(liveCandle.timestamp);
    if (time < lastSeriesTimeRef.current) return;
    seriesRef.current.update({
      time,
      open: Number(liveCandle.open),
      high: Number(liveCandle.high),
      low: Number(liveCandle.low),
      close: Number(liveCandle.close),
    });
    lastSeriesTimeRef.current = time;
  }, [liveCandle]);

  useEffect(() => {
    if (!ema9Ref.current) return;
    // indicators/ already comes back oldest-first from
    // load_candle_frame(), but sort defensively for the same reason
    // candles above does -- setData() silently no-ops on unsorted input.
    const sorted = [...indicators].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    ema9Ref.current.setData(toLinePoints(sorted, "ema9"));
    ema21Ref.current.setData(toLinePoints(sorted, "ema21"));
    sarRef.current.setData(toLinePoints(sorted, "sar"));
  }, [indicators]);

  return <div ref={containerRef} style={{ width: "100%" }} />;
}
