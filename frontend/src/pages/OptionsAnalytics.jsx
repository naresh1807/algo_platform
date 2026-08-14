import { useEffect, useMemo, useRef, useState } from "react";

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

// Thin background bar behind an OI number, width scaled to the max OI
// currently visible in the chain -- the same "scan the bars, not the
// digits" convention real broker option-chain screens use so OI
// concentration reads at a glance. `side` picks which edge the bar
// grows from (call side reads right-to-left toward the strike, put
// side left-to-right) so both halves visually point at the strike
// column between them.
function OiCell({ value, max, side }) {
  const pct = max > 0 && value != null ? Math.max(2, (value / max) * 100) : 0;
  return (
    <span className="chain-oi-bar-wrap">
      {pct > 0 && (
        <span
          className={`chain-oi-bar ${side === "put" ? "chain-oi-bar-red" : ""}`}
          style={{ [side === "call" ? "right" : "left"]: 0, width: `${pct}%` }}
        />
      )}
      <span className="chain-oi-value">{value != null ? value.toLocaleString() : "—"}</span>
    </span>
  );
}

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
  const [expiries, setExpiries] = useState([]);
  const [expiry, setExpiry] = useState("");
  const [chainRows, setChainRows] = useState([]);
  const [spot, setSpot] = useState(null);
  const [chainLoading, setChainLoading] = useState(false);
  const chainContainerRef = useRef(null);
  const atmRowRef = useRef(null);
  const [analytics, setAnalytics] = useState(null);
  const [error, setError] = useState(null);
  const [strikeDirection, setStrikeDirection] = useState("bullish");
  const [bestStrike, setBestStrike] = useState(null);
  const [bestStrikeLoading, setBestStrikeLoading] = useState(false);
  // Live-flash state: `${strike}-${side}` -> "up"|"down" for ~650ms
  // after a tick changes that cell's LTP -- the single most
  // recognizable "this is really moving" cue on a real broker chain
  // (Kite/Angel One both do this). prevLtpRef is a plain ref (no
  // re-render on write) since it's only ever read to DIFF against the
  // next tick, never rendered itself.
  const [flash, setFlash] = useState({});
  const prevLtpRef = useRef({});
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

  // Load which expiries actually have synced contracts for this
  // underlying, instead of a blind date-picker the user has to guess
  // a valid value for.
  useEffect(() => {
    let cancelled = false;
    setExpiry("");
    setChainRows([]);
    setAnalytics(null);
    endpoints.optionExpiries(underlying).then((res) => {
      // Guards against a slow response for a PREVIOUS underlying landing
      // after the user has already switched again -- without this, a
      // stale NIFTY expiries list could overwrite the BANKNIFTY one just
      // requested, offering expiries that don't belong to what's
      // actually selected.
      if (cancelled) return;
      const list = res.data.expiries ?? [];
      setExpiries(list);
      if (list.length > 0) setExpiry(list[0]);
    });
    return () => {
      cancelled = true;
    };
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
        setSpot(chainRes.value.data.spot ?? null);
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

  // Merge live snapshot pushes (apps/options/consumers.py) into the
  // already-loaded chain grid -- same "REST loads history, WebSocket
  // keeps it fresh" pattern as Dashboard.jsx's candle handling.
  useEffect(() => {
    if (!latestOptionUpdate) return;
    if (latestOptionUpdate.underlying !== underlying || latestOptionUpdate.expiry !== expiry) return;

    const side = latestOptionUpdate.option_type === "CE" ? "call" : "put";
    const flashKey = `${latestOptionUpdate.strike}-${side}`;
    const prevLtp = prevLtpRef.current[flashKey];
    if (prevLtp != null && latestOptionUpdate.ltp != null && latestOptionUpdate.ltp !== prevLtp) {
      const direction = latestOptionUpdate.ltp > prevLtp ? "up" : "down";
      setFlash((f) => ({ ...f, [flashKey]: direction }));
      setTimeout(() => {
        setFlash((f) => {
          if (f[flashKey] !== direction) return f; // a newer flash already replaced this one
          const { [flashKey]: _drop, ...rest } = f;
          return rest;
        });
      }, 650);
    }
    prevLtpRef.current[flashKey] = latestOptionUpdate.ltp;

    setChainRows((prev) => {
      const idx = prev.findIndex((r) => r.strike === latestOptionUpdate.strike);
      const updatedLeg = {
        ltp: latestOptionUpdate.ltp,
        open_interest: latestOptionUpdate.open_interest,
        change_in_oi: latestOptionUpdate.change_in_oi,
        volume: latestOptionUpdate.volume,
        iv: latestOptionUpdate.iv,
        bid: latestOptionUpdate.bid,
        ask: latestOptionUpdate.ask,
        timestamp: latestOptionUpdate.timestamp,
        greeks: latestOptionUpdate.greeks,
      };
      if (idx === -1) {
        const row = { strike: latestOptionUpdate.strike, call: null, put: null, [side]: updatedLeg };
        return [...prev, row].sort((a, b) => a.strike - b.strike);
      }
      const next = [...prev];
      next[idx] = { ...next[idx], [side]: updatedLeg };
      return next;
    });
  }, [latestOptionUpdate, underlying, expiry]);

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

  // Scales the OI depth bars behind each OI cell -- one shared max
  // across both call/put columns (not per-side) so a glance at bar
  // width is comparable across the whole visible ladder, matching how
  // a real chain's OI concentration reads at a glance.
  const maxOi = useMemo(() => {
    let max = 0;
    for (const row of chainRows) {
      if (row.call?.open_interest > max) max = row.call.open_interest;
      if (row.put?.open_interest > max) max = row.put.open_interest;
    }
    return max;
  }, [chainRows]);

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
          <h3 style={{ margin: 0 }}>
            Option Chain — {underlying}
            {spot != null && (
              <span style={{ fontSize: 13, fontWeight: 400, color: "var(--muted)", marginLeft: 10 }}>
                Spot {spot.toLocaleString(undefined, { minimumFractionDigits: 2 })}
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

        {expiries.length === 0 && (
          <p style={{ color: "var(--muted)", marginTop: 12 }}>
            No expiries synced yet for {underlying}. Run{" "}
            <code>python manage.py sync_option_contracts --underlying {underlying} --list-expiries</code>{" "}
            to see what's available, then sync one with --expiry.
          </p>
        )}
        {error && <p style={{ color: "var(--red)" }}>{error}</p>}

        {expiry && !chainLoading && chainRows.length > 0 && (
          <div ref={chainContainerRef} style={{ overflowX: "auto", overflowY: "auto", maxHeight: 480, marginTop: 12 }}>
            <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ color: "var(--muted)", textAlign: "right" }}>
                  <th className="chain-th">Δ</th>
                  <th className="chain-th">IV</th>
                  <th className="chain-th">OI</th>
                  <th className="chain-th">Chg OI</th>
                  <th className="chain-th">Vol</th>
                  <th className="chain-th">Bid</th>
                  <th className="chain-th">Ask</th>
                  <th className="chain-th">LTP</th>
                  <th className="chain-th" style={{ textAlign: "center" }}>Strike</th>
                  <th className="chain-th" style={{ textAlign: "left" }}>LTP</th>
                  <th className="chain-th" style={{ textAlign: "left" }}>Bid</th>
                  <th className="chain-th" style={{ textAlign: "left" }}>Ask</th>
                  <th className="chain-th" style={{ textAlign: "left" }}>Vol</th>
                  <th className="chain-th" style={{ textAlign: "left" }}>Chg OI</th>
                  <th className="chain-th" style={{ textAlign: "left" }}>OI</th>
                  <th className="chain-th" style={{ textAlign: "left" }}>IV</th>
                  <th className="chain-th" style={{ textAlign: "left" }}>Δ</th>
                </tr>
              </thead>
              <tbody>
                {chainRows.map((row) => {
                  const isAtm = row.strike === atmStrike;
                  // ITM = "in the money": a call is worth intrinsic value
                  // once spot has moved past its strike, a put the
                  // opposite way -- the light per-side tint real chains
                  // use so it's obvious which half of each row is
                  // currently "live" premium vs. mostly time value.
                  const callItm = spot != null && row.strike < spot;
                  const putItm = spot != null && row.strike > spot;
                  const callFlash = flash[`${row.strike}-call`];
                  const putFlash = flash[`${row.strike}-put`];
                  return (
                  <tr
                    key={row.strike}
                    ref={isAtm ? atmRowRef : null}
                    className="chain-row"
                    style={{
                      borderTop: "1px solid var(--border)",
                      background: isAtm ? "color-mix(in srgb, var(--accent) 14%, transparent)" : undefined,
                    }}
                  >
                    <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)", background: callItm && !isAtm ? "color-mix(in srgb, var(--green) 6%, transparent)" : undefined }}>
                      {row.call?.greeks?.delta != null ? row.call.greeks.delta.toFixed(2) : "—"}
                    </td>
                    <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)" }}>{row.call?.iv ?? "—"}</td>
                    <td className="chain-td" style={{ textAlign: "right" }}><OiCell value={row.call?.open_interest} max={maxOi} side="call" /></td>
                    <td className="chain-td" style={{ textAlign: "right", color: (row.call?.change_in_oi ?? 0) >= 0 ? "var(--green)" : "var(--red)" }}>
                      {row.call?.change_in_oi?.toLocaleString() ?? "—"}
                    </td>
                    <td className="chain-td" style={{ textAlign: "right" }}>{row.call?.volume?.toLocaleString() ?? "—"}</td>
                    <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)" }}>{row.call?.bid ?? "—"}</td>
                    <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)" }}>{row.call?.ask ?? "—"}</td>
                    <td className={`chain-td ${callFlash === "up" ? "chain-flash-up" : callFlash === "down" ? "chain-flash-down" : ""}`} style={{ textAlign: "right", fontWeight: 600 }}>
                      {row.call?.ltp ?? "—"}
                    </td>
                    <td className="chain-td" style={{ textAlign: "center" }}>
                      <span style={{ fontWeight: 600, color: isAtm ? "var(--accent)" : undefined }}>{row.strike}</span>
                      {isAtm && <span className="badge badge-accent" style={{ marginLeft: 6 }}>ATM</span>}
                    </td>
                    <td className={`chain-td ${putFlash === "up" ? "chain-flash-up" : putFlash === "down" ? "chain-flash-down" : ""}`} style={{ fontWeight: 600 }}>
                      {row.put?.ltp ?? "—"}
                    </td>
                    <td className="chain-td" style={{ color: "var(--muted)" }}>{row.put?.bid ?? "—"}</td>
                    <td className="chain-td" style={{ color: "var(--muted)" }}>{row.put?.ask ?? "—"}</td>
                    <td className="chain-td">{row.put?.volume?.toLocaleString() ?? "—"}</td>
                    <td className="chain-td" style={{ color: (row.put?.change_in_oi ?? 0) >= 0 ? "var(--green)" : "var(--red)" }}>
                      {row.put?.change_in_oi?.toLocaleString() ?? "—"}
                    </td>
                    <td className="chain-td" style={{ background: putItm && !isAtm ? "color-mix(in srgb, var(--red) 6%, transparent)" : undefined }}>
                      <OiCell value={row.put?.open_interest} max={maxOi} side="put" />
                    </td>
                    <td className="chain-td" style={{ color: "var(--muted)" }}>{row.put?.iv ?? "—"}</td>
                    <td className="chain-td" style={{ color: "var(--muted)" }}>
                      {row.put?.greeks?.delta != null ? row.put.greeks.delta.toFixed(2) : "—"}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
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
    </div>
  );
}
