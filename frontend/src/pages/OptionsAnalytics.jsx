import { useEffect, useMemo, useRef, useState } from "react";

import OptionChainTable from "../components/OptionChainTable.jsx";
import OptionContractChartModal from "../components/OptionContractChartModal.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import EmptyState from "../components/EmptyState.jsx";
import { useExpiryStatus } from "../hooks/useExpiryStatus.js";
import { useLiveIndexSpot } from "../hooks/useLiveIndexSpot.js";
import { useLiveOptionChainMerge } from "../hooks/useLiveOptionChainMerge.js";
import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";

const SIGNAL_LABELS = {
  bullish_call_buildup: "Bullish Call Buildup",
  bearish_put_buildup: "Bearish Put Buildup",
  short_covering: "Short Covering",
  long_unwinding: "Long Unwinding",
  expiry_pinning_risk: "Expiry Pinning Risk",
  iv_crush_warning: "IV Crush Warning",
  high_decay_no_trade_zone: "High-Decay No-Trade Zone",
};

const selectStyle = {
  background: "var(--panel)", color: "var(--text)",
  border: "1px solid var(--border)", borderRadius: 6, padding: 6,
};

/**
 * manual section 6: "Options Analytics" + the option-chain grid itself
 * (CE | Strike | PE, Angel One's own chart-page layout). Two data
 * sources feed this page:
 *   - OptionChainView (apps/options/views.py `/options/chain/`) for the
 *     strike-by-strike CE/PE grid
 *   - OptionsAnalyticsView (`/options/analytics/`) for the derived
 *     PCR/max-pain/support-resistance/signals panels below it
 * Both need OptionContract rows to already exist for the selected
 * expiry (`python manage.py sync_option_contracts`) and BROKER_MODE=live
 * for snapshots to ever be non-empty -- see that command's docstring.
 */
export default function OptionsAnalytics() {
  const [underlying, setUnderlying] = useState("NIFTY");
  // Single shared source of truth for expiry selection (apps.options.
  // expiry_service via /api/options/expiry-status/) -- see
  // hooks/useExpiryStatus.js's own docstring for why this replaced this
  // page's own "fetch expiries, auto-select first, never re-validate"
  // effect (the same duplicated logic JarvisSignalTerminal.jsx used to
  // carry independently).
  const {
    expiry, setExpiry, availableExpiries: expiries, lastSuccessfulSync,
    rolloverRequired, unavailable: expiryUnavailable, error: expiryError,
  } = useExpiryStatus(underlying);
  const [chainRows, setChainRows] = useState([]);
  // restSpot is the one-time snapshot from the /options/chain/ REST
  // response (real, but only as fresh as the last fetch). `spot`
  // prefers a live index-price tick (useLiveIndexSpot, the same
  // /ws/investing/index-live/ push IndexTickerBar/TopBar already use)
  // once one arrives, falling back to restSpot until then -- so the
  // displayed spot never silently goes stale for the rest of the
  // session the way it used to (individual option premiums already
  // updated live via latestOptionUpdate; the underlying's own price
  // didn't).
  const [restSpot, setRestSpot] = useState(null);
  const liveSpot = useLiveIndexSpot(underlying);
  const spot = liveSpot ?? restSpot;
  const [chainLoading, setChainLoading] = useState(false);
  const chainContainerRef = useRef(null);
  const atmRowRef = useRef(null);
  const [analytics, setAnalytics] = useState(null);
  const [error, setError] = useState(null);
  const [strikeDirection, setStrikeDirection] = useState("bullish");
  const [bestStrike, setBestStrike] = useState(null);
  const [bestStrikeLoading, setBestStrikeLoading] = useState(false);
  // {strike, optionType, underlying, expiry} | null -- which contract's
  // own price-history chart the click-a-strike popup is showing.
  const [selectedContract, setSelectedContract] = useState(null);
  // Live-flash state: `${strike}-${side}` -> "up"|"down" for ~650ms
  // after a tick changes that cell's LTP -- the single most
  // recognizable "this is really moving" cue on a real broker chain
  // (Kite/Angel One both do this). prevLtpRef is a plain ref (no
  // re-render on write) since it's only ever read to DIFF against the
  // next tick, never rendered itself.
  const [flash, setFlash] = useState({});
  // Guards fetchBestStrike against out-of-order responses: clicking
  // Bullish then Bearish before the first request resolves previously
  // let the slower Bullish response land last and overwrite `bestStrike`
  // with a CE suggestion while `strikeDirection` (set synchronously on
  // click) already said "bearish" -- rendering showed the bullish
  // strike labeled PE. Only the response matching the MOST RECENT
  // request is ever applied.
  const bestStrikeRequestRef = useRef(0);

  const latestOptionUpdate = useLiveStore((s) => s.latestOptionUpdate);

  // Live sockets are connected once at the AppShell level (App.jsx),
  // not per-page -- see liveStore.js's module docstring for why.

  // Chain/analytics reset whenever the underlying changes -- the expiry
  // itself is now owned by useExpiryStatus above (it re-defaults on
  // underlying change too, see that hook's own effect).
  useEffect(() => {
    setChainRows([]);
    setAnalytics(null);
  }, [underlying]);

  useEffect(() => {
    if (!expiry) return;
    let cancelled = false;
    setError(null);
    setChainLoading(true);

    Promise.allSettled([
      endpoints.optionChain(underlying, expiry),
      endpoints.optionsAnalytics(underlying, expiry),
    ]).then(([chainRes, analyticsRes]) => {
      // Same race as the expiries effect above: rapidly switching
      // underlying/expiry can let an older, slower request resolve
      // AFTER a newer one already populated the chain, silently
      // replacing the currently-selected underlying's chain/analytics
      // with a different underlying's stale data.
      if (cancelled) return;
      setChainLoading(false);
      if (chainRes.status === "fulfilled") {
        setChainRows(chainRes.value.data.rows ?? []);
        setRestSpot(chainRes.value.data.spot ?? null);
      } else {
        setError(
          "Could not load the option chain -- make sure OptionContract rows exist for this " +
            "underlying/expiry (python manage.py sync_option_contracts) and the ingestion task has run."
        );
      }
      if (analyticsRes.status === "fulfilled") setAnalytics(analyticsRes.value.data);
    });
    return () => {
      cancelled = true;
    };
  }, [underlying, expiry]);

  // Merge live snapshot/LTP pushes (apps/options/consumers.py,
  // apps/options/live_feed.py) into the already-loaded chain grid --
  // same "REST loads history, WebSocket keeps it fresh" pattern as
  // Dashboard.jsx's candle handling. See hooks/useLiveOptionChainMerge.js
  // for why this must be a field-level merge, not a whole-leg replace.
  useLiveOptionChainMerge({ latestOptionUpdate, underlying, expiry, setChainRows, setFlash });

  const fetchBestStrike = (direction) => {
    if (!expiry) return;
    const requestId = ++bestStrikeRequestRef.current;
    setBestStrikeLoading(true);
    setStrikeDirection(direction);
    endpoints.bestStrike(underlying, expiry, direction).then((res) => {
      if (requestId !== bestStrikeRequestRef.current) return; // superseded by a later click
      setBestStrike(res.data);
      setBestStrikeLoading(false);
    }).catch(() => {
      if (requestId !== bestStrikeRequestRef.current) return;
      setBestStrikeLoading(false);
    });
  };

  useEffect(() => {
    setBestStrike(null);
  }, [underlying, expiry]);

  // The strike closest to the live underlying price -- "ATM" (at the
  // money) in options terminology. chainRows is already strike-sorted
  // ascending (see the WebSocket-merge effect above, which re-sorts on
  // every update), so this is a simple nearest-value reduce, not a
  // search.
  const atmStrike = useMemo(() => {
    if (spot == null || chainRows.length === 0) return null;
    return chainRows.reduce(
      (closest, row) => (Math.abs(row.strike - spot) < Math.abs(closest - spot) ? row.strike : closest),
      chainRows[0].strike
    );
  }, [chainRows, spot]);

  // Auto-scroll the chain to the current price when it's OPENED --
  // real broker option-chain screens (Kite, Angel One) open already
  // centered on ATM rather than making the trader scroll through 50+
  // strikes to find where price actually is. hasAutoScrolledRef makes
  // this a ONE-TIME scroll per underlying/expiry selection, not a
  // continuous re-center on every live tick -- price moving enough to
  // shift which strike is nearest shouldn't yank a trader's view back
  // to ATM if they've since scrolled elsewhere to look at other
  // strikes. Scrolls chainContainerRef's scrollTop directly (not
  // atmRowRef.scrollIntoView(), which can also drag the outer page's
  // own scroll position along with it) so only the chain's own
  // scrollable box moves.
  const hasAutoScrolledRef = useRef(false);
  useEffect(() => {
    hasAutoScrolledRef.current = false;
  }, [underlying, expiry]);
  useEffect(() => {
    if (hasAutoScrolledRef.current) return;
    if (atmStrike == null || !chainContainerRef.current || !atmRowRef.current) return;
    const container = chainContainerRef.current;
    const row = atmRowRef.current;
    container.scrollTop = row.offsetTop - container.clientHeight / 2 + row.clientHeight / 2;
    hasAutoScrolledRef.current = true;
  }, [atmStrike]);

  return (
    <div>
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span>
              Option Chain — {underlying}
              {spot != null && (
                <span style={{ fontSize: 13, fontWeight: 400, color: "var(--muted)", marginLeft: 10 }}>
                  Spot {spot.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                </span>
              )}
            </span>
            {rolloverRequired && !expiryUnavailable && <StatusBadge tone="warn">Rollover pending</StatusBadge>}
            {lastSuccessfulSync && (
              <span style={{ fontSize: 11, fontWeight: 400, color: "var(--muted)" }}>
                Contracts synced {new Date(lastSuccessfulSync).toLocaleString()}
              </span>
            )}
          </h3>
          <div style={{ display: "flex", gap: 12 }}>
            <select value={underlying} onChange={(e) => setUnderlying(e.target.value)} style={selectStyle}>
              <option value="NIFTY">NIFTY</option>
              <option value="BANKNIFTY">BANKNIFTY</option>
            </select>
            <select
              value={expiry}
              onChange={(e) => setExpiry(e.target.value)}
              style={selectStyle}
              disabled={expiries.length === 0}
            >
              {expiries.length === 0 ? (
                <option value="">No synced expiries</option>
              ) : (
                expiries.map((e) => <option key={e} value={e}>{e}</option>)
              )}
            </select>
          </div>
        </div>

        {expiryError && (
          <EmptyState title="Could not load expiry information" detail={expiryError} />
        )}
        {!expiryError && expiryUnavailable && (
          <EmptyState
            title="Contract sync temporarily unavailable"
            detail={`No valid, non-expired expiry is available for ${underlying} right now -- a rollover resync is required and should complete automatically shortly.`}
          />
        )}
        {!expiryError && !expiryUnavailable && expiries.length === 0 && (
          <p style={{ color: "var(--muted)", marginTop: 12 }}>
            No expiries synced yet for {underlying}. Run{" "}
            <code>python manage.py sync_option_contracts --underlying {underlying} --list-expiries</code>{" "}
            to see what's available, then sync one with --expiry.
          </p>
        )}
        {error && <p style={{ color: "var(--red)" }}>{error}</p>}

        {expiry && !chainLoading && chainRows.length > 0 && (
          <OptionChainTable
            rows={chainRows}
            spot={spot}
            atmStrike={atmStrike}
            flash={flash}
            containerRef={chainContainerRef}
            atmRowRef={atmRowRef}
            onCellClick={(strike, optionType) => setSelectedContract({ strike, optionType })}
          />
        )}
        {expiry && !chainLoading && chainRows.length === 0 && !error && (
          <p style={{ color: "var(--muted)", marginTop: 12 }}>
            Contracts are synced but no snapshots yet -- run the option-chain ingestion task
            (BROKER_MODE=live, waits on Celery beat) or trigger it manually once.
          </p>
        )}
        {chainLoading && <p style={{ color: "var(--muted)", marginTop: 12 }}>Loading chain…</p>}
      </div>

      {analytics && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
            <div className="panel">
              <h3>Put-Call Ratio</h3>
              <p style={{ fontSize: 24 }}>{analytics.pcr ?? "—"}</p>
              <p style={{ fontSize: 12, color: "var(--muted)" }}>
                {">"}1 conventionally bullish, {"<"}1 conventionally bearish (by OI)
              </p>
            </div>
            <div className="panel">
              <h3>Max Pain</h3>
              <p style={{ fontSize: 24 }}>{analytics.max_pain ?? "—"}</p>
              <p style={{ fontSize: 12, color: "var(--muted)" }}>
                Strike where option writers collectively lose the least
              </p>
            </div>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
            <div className="panel">
              <h3>Support (highest Put OI)</h3>
              {analytics.support_resistance.support.length === 0 ? (
                <p style={{ color: "var(--muted)" }}>No data.</p>
              ) : (
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                  {analytics.support_resistance.support.map((s) => (
                    <li key={s.strike}>{s.strike} — OI {s.oi.toLocaleString()}</li>
                  ))}
                </ul>
              )}
            </div>
            <div className="panel">
              <h3>Resistance (highest Call OI)</h3>
              {analytics.support_resistance.resistance.length === 0 ? (
                <p style={{ color: "var(--muted)" }}>No data.</p>
              ) : (
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                  {analytics.support_resistance.resistance.map((s) => (
                    <li key={s.strike}>{s.strike} — OI {s.oi.toLocaleString()}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div className="panel" style={{ marginTop: 12 }}>
            <h3>Options Signals</h3>
            {Object.entries(analytics.signals).map(([key, { flag, detail }]) => (
              <div
                key={key}
                style={{
                  display: "flex", gap: 10, alignItems: "flex-start",
                  padding: "6px 0", borderTop: "1px solid var(--border)",
                }}
              >
                <span className={`status-dot ${flag ? "warn" : "ok"}`} style={{ marginTop: 5 }} />
                <div>
                  <div style={{ fontWeight: flag ? 600 : 400 }}>{SIGNAL_LABELS[key] || key}</div>
                  {flag && detail && (
                    <div style={{ fontSize: 12, color: "var(--muted)" }}>{detail}</div>
                  )}
                </div>
              </div>
            ))}
          </div>

          <div className="panel" style={{ marginTop: 12 }}>
            <h3>Best Strike Suggestion</h3>
            <p style={{ fontSize: 12, color: "var(--muted)" }}>
              Ranks strikes by delta (probability proxy), OI/volume (liquidity), and theta decay --
              see apps.options.strike_selector for the scoring.
            </p>
            <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
              <button
                onClick={() => fetchBestStrike("bullish")}
                style={{
                  padding: "4px 10px", borderRadius: 6, cursor: "pointer",
                  border: `1px solid ${strikeDirection === "bullish" ? "var(--green)" : "var(--border)"}`,
                  background: "transparent", color: strikeDirection === "bullish" ? "var(--green)" : "var(--text)",
                }}
              >
                Bullish (Call)
              </button>
              <button
                onClick={() => fetchBestStrike("bearish")}
                style={{
                  padding: "4px 10px", borderRadius: 6, cursor: "pointer",
                  border: `1px solid ${strikeDirection === "bearish" ? "var(--red)" : "var(--border)"}`,
                  background: "transparent", color: strikeDirection === "bearish" ? "var(--red)" : "var(--text)",
                }}
              >
                Bearish (Put)
              </button>
            </div>
            {bestStrikeLoading && <p style={{ color: "var(--muted)" }}>Analyzing strikes…</p>}
            {!bestStrikeLoading && bestStrike && (
              <div>
                {bestStrike.suggested ? (
                  <>
                    <p style={{ fontSize: 20, fontWeight: 600 }}>
                      {bestStrike.suggested.strike} {strikeDirection === "bullish" ? "CE" : "PE"}
                    </p>
                    <p style={{ fontSize: 13 }}>{bestStrike.reason}</p>
                  </>
                ) : (
                  <p style={{ color: "var(--muted)", fontSize: 13 }}>{bestStrike.reason}</p>
                )}
              </div>
            )}
          </div>
        </>
      )}

      <OptionContractChartModal
        contract={selectedContract}
        underlying={underlying}
        expiry={expiry}
        chainRows={chainRows}
        onClose={() => setSelectedContract(null)}
      />
    </div>
  );
}
