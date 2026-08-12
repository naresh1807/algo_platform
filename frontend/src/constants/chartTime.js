/**
 * Shared IST-aware time formatting for every lightweight-charts instance
 * on the dashboard (PriceChart, RSIPanel, MACDPanel) -- kept in one
 * place so all three panels' time axes stay in agreement. Two things a
 * naive setup gets wrong, both are real bugs this fixes:
 *
 * 1. lightweight-charts formats its time axis and crosshair tooltip in
 *    UTC (or the browser's local timezone if left fully default) by
 *    default, not IST -- NSE candle times are IST, so without an
 *    explicit formatter every label would be off by up to 5:30 hours
 *    from what the candle actually represents.
 * 2. A naive custom tickMarkFormatter that always renders "HH:mm" loses
 *    the DATE entirely -- real broker/charting platforms (Kite, Angel
 *    One, TradingView) switch to showing the date on ticks that land on
 *    a day boundary or when the axis is zoomed out past intraday
 *    resolution, so a trader scrolling back through multiple days can
 *    tell which day they're looking at without hovering for the
 *    tooltip. tickMarkFormatter's second argument (tickMarkType) is
 *    exactly what tells us which case applies.
 */

import { TickMarkType } from "lightweight-charts";

export const IST_TIME_ZONE = "Asia/Kolkata";

export const formatIstTick = (utcSeconds, tickMarkType) => {
  const date = new Date(utcSeconds * 1000);
  switch (tickMarkType) {
    case TickMarkType.Year:
      return date.toLocaleDateString("en-GB", { timeZone: IST_TIME_ZONE, year: "numeric" });
    case TickMarkType.Month:
      return date.toLocaleDateString("en-GB", { timeZone: IST_TIME_ZONE, month: "short", year: "numeric" });
    case TickMarkType.DayOfMonth:
      return date.toLocaleDateString("en-GB", { timeZone: IST_TIME_ZONE, day: "2-digit", month: "short" });
    default:
      return date.toLocaleTimeString("en-GB", {
        timeZone: IST_TIME_ZONE, hour: "2-digit", minute: "2-digit", hour12: false,
      });
  }
};

export const formatIstTooltip = (utcSeconds) =>
  new Date(utcSeconds * 1000).toLocaleString("en-GB", {
    timeZone: IST_TIME_ZONE,
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }) + " IST";

// Standard options object every panel's timeScale should spread in --
// keeps the date-tick behavior and IST tooltip identical across panels
// without each one re-specifying it slightly differently.
export function istTimeScaleOptions(extra = {}) {
  return {
    timeVisible: true,
    secondsVisible: false,
    tickMarkFormatter: formatIstTick,
    ...extra,
  };
}

export const istLocalizationOptions = { timeFormatter: formatIstTooltip };
