import { useMemo, useState } from "react";
import { X } from "lucide-react";

import MACDPanel from "../charts/MACDPanel.jsx";
import OptionCandleChart from "../charts/OptionCandleChart.jsx";
import RSIPanel from "../charts/RSIPanel.jsx";
import EmptyState from "./EmptyState.jsx";
import ErrorState from "./ErrorState.jsx";
import IndicatorToggles from "./IndicatorToggles.jsx";
import LoadingSkeleton from "./LoadingSkeleton.jsx";
import TimeframeSelector from "./TimeframeSelector.jsx";
import { TIMEFRAMES } from "../constants/market.js";
import { useOptionCandles } from "../hooks/useOptionCandles.js";
import { useThemeStore } from "../store/themeStore.js";
import { computeOptionIndicators, computeSupertrend, computeVWAP } from "../utils/optionIndicators.js";

const DEFAULT_TOGGLES = { ema9: true, ema21: true, vwap: true, supertrend: false, rsi: false, macd: false, volume: true };

// A subset of TIMEFRAMES -- an option contract chart doesn't need the
// underlying's full 1m-1d range; 1h/1d option candles are rarely
// useful for an intraday premium chart and would mostly just show
// sparse bars. Matches WORKSPACE_TIMEFRAMES' own "curated subset, not
// the full list" convention in JarvisSignalTerminal.jsx.
const CONTRACT_TIMEFRAMES = TIMEFRAMES.filter((t) => ["1m", "5m", "15m", "30m"].includes(t.value));

function mergeLiveCandle(candles, liveCandle) {
  if (!liveCandle) return candles;
  if (candles.length === 0) return [{ time: liveCandle.timestamp, open: liveCandle.open, high: liveCandle.high, low: liveCandle.low, close: liveCandle.close, volume: liveCandle.volume }];
  const lastTime = new Date(candles[candles.length - 1].time).getTime();
  const liveTime = new Date(liveCandle.timestamp).getTime();
  const bar = { time: liveCandle.timestamp, open: liveCandle.open, high: liveCandle.high, low: liveCandle.low, close: liveCandle.close, volume: liveCandle.volume };
  if (liveTime === lastTime) return [...candles.slice(0, -1), bar];
  if (liveTime > lastTime) return [...candles, bar];
  return candles;
}

/**
 * Broker-app-parity popup (manual's "click a strike to see its own
 * chart" convention) -- opened from the option chain table's CE/PE LTP
 * cells. Renders the SELECTED OPTION CONTRACT's own real OHLC
 * candlestick chart (never the underlying's, never a line synthesized
 * from LTP alone -- apps.options.candle_service is the backend's real
 * Angel One-backed candle history for exactly this contract's own
 * symbol_token) with volume and toggleable indicators, replacing the
 * previous OptionPriceChart line-only view.
 *
 * `underlying`/`expiry` are the PAGE's live current values (not frozen
 * at click time) so a rollover that happens while this modal is open
 * is picked up automatically -- see useOptionCandles.js's own docstring
 * for the same-side nearest-strike fallback this triggers if the exact
 * strike no longer exists on the new expiry. `chainRows` is the page's
 * already-loaded chain for `expiry`, used only for that fallback.
 */
export default function OptionContractChartModal({ contract, underlying, expiry, chainRows = [], onClose }) {
  const theme = useThemeStore((s) => s.theme);
  const [timeframe, setTimeframe] = useState("5m");
  const [chartType, setChartType] = useState("candlestick");
  const [toggles, setToggles] = useState(DEFAULT_TOGGLES);

  const strike = contract?.strike ?? null;
  const optionType = contract?.optionType ?? null;

  const { contract: resolved, candles, liveCandle, loading, error, stale } = useOptionCandles({
    underlying: contract ? underlying : null,
    expiry: contract ? expiry : null,
    strike,
    optionType,
    timeframe,
    chainRows,
  });

  const merged = useMemo(() => mergeLiveCandle(candles, liveCandle), [candles, liveCandle]);
  const optionIndicators = useMemo(() => computeOptionIndicators(merged), [merged]);
  const vwapSeries = useMemo(() => (toggles.vwap ? computeVWAP(merged) : []), [merged, toggles.vwap]);
  const supertrendSeries = useMemo(() => (toggles.supertrend ? computeSupertrend(merged) : []), [merged, toggles.supertrend]);

  const toggleIndicator = (key) => setToggles((prev) => ({ ...prev, [key]: !prev[key] }));

  if (!contract) return null;

  const titleId = "option-contract-modal-title";
  const label = resolved
    ? `${resolved.underlying} ${resolved.strike} ${resolved.option_type}`
    : `${underlying ?? ""} ${strike ?? ""} ${optionType ?? ""}`;

  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "var(--overlay-scrim)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        className="panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        style={{ width: "min(920px, 95vw)", margin: 0 }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 id={titleId} style={{ margin: 0 }}>
            {label}
            <span style={{ fontSize: 12, fontWeight: 400, color: "var(--muted)", marginLeft: 8 }}>
              Expiry: {resolved?.expiry ?? expiry} | {timeframe}
            </span>
            {stale && (
              <span className="badge badge-red" style={{ marginLeft: 8, fontSize: 10 }} title="Live feed reconnecting -- candles may be momentarily stale">
                Reconnecting…
              </span>
            )}
          </h3>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8, margin: "10px 0" }}>
          <IndicatorToggles toggles={toggles} onToggle={toggleIndicator} />
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <div role="group" aria-label="Chart type" style={{ display: "inline-flex", gap: 2, background: "var(--elevated)", padding: 3, borderRadius: "var(--radius-sm)" }}>
              {[{ value: "candlestick", label: "Candles" }, { value: "line", label: "Line" }].map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setChartType(opt.value)}
                  aria-pressed={chartType === opt.value}
                  style={{
                    border: "none", borderRadius: 6, padding: "5px 10px", fontSize: 12, fontWeight: 600, cursor: "pointer",
                    background: chartType === opt.value ? "var(--accent)" : "transparent",
                    color: chartType === opt.value ? "var(--accent-contrast)" : "var(--muted)",
                  }}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <TimeframeSelector options={CONTRACT_TIMEFRAMES} value={timeframe} onChange={setTimeframe} />
          </div>
        </div>

        {loading ? (
          <LoadingSkeleton height={380} />
        ) : error ? (
          <ErrorState title="Couldn't load this contract's chart" detail={error} />
        ) : merged.length === 0 ? (
          <EmptyState
            title={`No ${label} ${timeframe} candles yet`}
            detail="Angel One has no historical data for this contract yet -- check back after the next ingestion cycle, or try a different timeframe."
          />
        ) : (
          <>
            <OptionCandleChart
              candles={candles}
              liveCandle={liveCandle}
              indicators={optionIndicators}
              vwap={vwapSeries}
              supertrend={supertrendSeries}
              theme={theme}
              chartType={chartType}
              toggles={toggles}
              height={380}
            />
            {toggles.rsi && (
              <div style={{ marginTop: 8 }}>
                <RSIPanel indicators={optionIndicators} theme={theme} />
              </div>
            )}
            {toggles.macd && (
              <div style={{ marginTop: 8 }}>
                <MACDPanel indicators={optionIndicators} theme={theme} />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
