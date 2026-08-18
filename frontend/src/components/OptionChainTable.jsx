import { useMemo } from "react";

// Thin background bar behind an OI number, width scaled to the max OI
// currently visible in the chain -- the same "scan the bars, not the
// digits" convention real broker option-chain screens use so OI
// concentration reads at a glance. `side` picks which edge the bar
// grows from (call side reads right-to-left toward the strike, put
// side left-to-right) so both halves visually point at the strike
// column between them.
function OiCell({ value, max, side, highlight }) {
  const pct = max > 0 && value != null ? Math.max(2, (value / max) * 100) : 0;
  return (
    <span className="chain-oi-bar-wrap">
      {pct > 0 && (
        <span
          className={`chain-oi-bar ${side === "put" ? "chain-oi-bar-red" : ""}`}
          style={{ [side === "call" ? "right" : "left"]: 0, width: `${pct}%` }}
        />
      )}
      <span className="chain-oi-value" style={{ fontWeight: highlight ? 700 : 400 }}>
        {value != null ? value.toLocaleString() : "—"}
      </span>
    </span>
  );
}

/**
 * Extracted from frontend/src/pages/OptionsAnalytics.jsx (previously
 * inline in that 538-line page) so a second page (the JARVIS signal
 * terminal) can reuse the exact same chain rendering -- sticky-ish
 * scrollable table, ATM highlight, OI depth bars, live-flash-on-LTP-
 * change -- without duplicating it. OptionsAnalytics.jsx now imports
 * this component too; its own behavior is unchanged, just relocated.
 *
 * New on top of the original inline version: gamma/theta/vega columns
 * (the backend's OptionChainView response already includes these in
 * each leg's `greeks` object -- only `delta` was ever rendered before),
 * highest-OI highlighting, and an AI-selected-strike / executing-
 * contract highlight (for the signal terminal's "which contract did
 * JARVIS pick" use case, via the aiSelectedStrike/selectedContractStrike
 * props -- OptionsAnalytics.jsx simply doesn't pass those, so they're
 * always inert there). The extra greeks columns and OI highlighting
 * are NOT prop-gated -- they benefit OptionsAnalytics.jsx too, since
 * this is now the one shared chain table both pages render, and having
 * two divergent versions would defeat the point of extracting it.
 *
 * Purely a rendering component: `rows`/`flash` are owned by the caller
 * (which already has its own REST-load + live-WebSocket-merge effects
 * for this data, following the pattern liveStore.js's own docstring
 * describes) -- this component does no data fetching of its own.
 */
export default function OptionChainTable({
  rows,
  spot,
  atmStrike,
  flash = {},
  aiSelectedStrike = null,
  aiSelectedSide = null,
  selectedContractStrike = null,
  selectedContractSide = null,
  onCellClick,
  containerRef,
  atmRowRef,
  maxHeight = 480,
}) {
  const maxOi = useMemo(() => {
    let max = 0;
    for (const row of rows) {
      if (row.call?.open_interest > max) max = row.call.open_interest;
      if (row.put?.open_interest > max) max = row.put.open_interest;
    }
    return max;
  }, [rows]);

  const isHighestOi = (value) => value != null && maxOi > 0 && value === maxOi;

  if (rows.length === 0) return null;

  return (
    <div ref={containerRef} style={{ overflowX: "auto", overflowY: "auto", maxHeight, marginTop: 12 }}>
      <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ color: "var(--muted)", textAlign: "right" }}>
            <th className="chain-th">Vega</th>
            <th className="chain-th">Theta</th>
            <th className="chain-th">Gamma</th>
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
            <th className="chain-th" style={{ textAlign: "left" }}>Gamma</th>
            <th className="chain-th" style={{ textAlign: "left" }}>Theta</th>
            <th className="chain-th" style={{ textAlign: "left" }}>Vega</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isAtm = row.strike === atmStrike;
            const isAiCall = aiSelectedSide === "CE" && row.strike === aiSelectedStrike;
            const isAiPut = aiSelectedSide === "PE" && row.strike === aiSelectedStrike;
            const isSelectedCall = selectedContractSide === "CE" && row.strike === selectedContractStrike;
            const isSelectedPut = selectedContractSide === "PE" && row.strike === selectedContractStrike;
            // ITM = "in the money": a call is worth intrinsic value
            // once spot has moved past its strike, a put the
            // opposite way -- the light per-side tint real chains
            // use so it's obvious which half of each row is
            // currently "live" premium vs. mostly time value.
            const callItm = spot != null && row.strike < spot;
            const putItm = spot != null && row.strike > spot;
            const callFlash = flash[`${row.strike}-call`];
            const putFlash = flash[`${row.strike}-put`];
            const rowHighlighted = isAtm || isAiCall || isAiPut || isSelectedCall || isSelectedPut;
            return (
              <tr
                key={row.strike}
                ref={isAtm ? atmRowRef : null}
                className="chain-row"
                style={{
                  borderTop: "1px solid var(--border)",
                  background: rowHighlighted
                    ? "color-mix(in srgb, var(--accent) 14%, transparent)"
                    : undefined,
                  outline: isSelectedCall || isSelectedPut ? "1px solid var(--accent)" : undefined,
                }}
              >
                <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)" }}>
                  {row.call?.greeks?.vega != null ? row.call.greeks.vega.toFixed(2) : "—"}
                </td>
                <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)" }}>
                  {row.call?.greeks?.theta != null ? row.call.greeks.theta.toFixed(2) : "—"}
                </td>
                <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)" }}>
                  {row.call?.greeks?.gamma != null ? row.call.greeks.gamma.toFixed(4) : "—"}
                </td>
                <td
                  className="chain-td"
                  style={{
                    textAlign: "right", color: "var(--muted)",
                    background: callItm && !isAtm ? "color-mix(in srgb, var(--green) 6%, transparent)" : undefined,
                  }}
                >
                  {row.call?.greeks?.delta != null ? row.call.greeks.delta.toFixed(2) : "—"}
                </td>
                <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)" }}>{row.call?.iv ?? "—"}</td>
                <td className="chain-td" style={{ textAlign: "right" }}>
                  <OiCell value={row.call?.open_interest} max={maxOi} side="call" highlight={isHighestOi(row.call?.open_interest)} />
                </td>
                <td className="chain-td" style={{ textAlign: "right", color: (row.call?.change_in_oi ?? 0) >= 0 ? "var(--green)" : "var(--red)" }}>
                  {row.call?.change_in_oi?.toLocaleString() ?? "—"}
                </td>
                <td className="chain-td" style={{ textAlign: "right" }}>{row.call?.volume?.toLocaleString() ?? "—"}</td>
                <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)" }}>{row.call?.bid ?? "—"}</td>
                <td className="chain-td" style={{ textAlign: "right", color: "var(--muted)" }}>{row.call?.ask ?? "—"}</td>
                <td
                  className={`chain-td ${callFlash === "up" ? "chain-flash-up" : callFlash === "down" ? "chain-flash-down" : ""}`}
                  style={{ textAlign: "right", fontWeight: 600, cursor: row.call?.ltp != null ? "pointer" : undefined }}
                  title={row.call?.ltp != null ? "View this contract's price chart" : undefined}
                  onClick={() => row.call?.ltp != null && onCellClick?.(row.strike, "CE")}
                >
                  {row.call?.ltp ?? "—"}
                  {(isAiCall || isSelectedCall) && (
                    <span
                      className={`badge ${isSelectedCall ? "badge-accent" : "badge-green"}`}
                      style={{ marginLeft: 6, fontSize: 9 }}
                      title={isSelectedCall ? "Contract JARVIS selected for this signal" : "AI-selected strike"}
                    >
                      {isSelectedCall ? "SELECTED" : "AI"}
                    </span>
                  )}
                </td>
                <td className="chain-td" style={{ textAlign: "center" }}>
                  <span style={{ fontWeight: 600, color: isAtm ? "var(--accent)" : undefined }}>{row.strike}</span>
                  {isAtm && <span className="badge badge-accent" style={{ marginLeft: 6 }}>ATM</span>}
                </td>
                <td
                  className={`chain-td ${putFlash === "up" ? "chain-flash-up" : putFlash === "down" ? "chain-flash-down" : ""}`}
                  style={{ fontWeight: 600, cursor: row.put?.ltp != null ? "pointer" : undefined }}
                  title={row.put?.ltp != null ? "View this contract's price chart" : undefined}
                  onClick={() => row.put?.ltp != null && onCellClick?.(row.strike, "PE")}
                >
                  {row.put?.ltp ?? "—"}
                  {(isAiPut || isSelectedPut) && (
                    <span
                      className={`badge ${isSelectedPut ? "badge-accent" : "badge-green"}`}
                      style={{ marginLeft: 6, fontSize: 9 }}
                      title={isSelectedPut ? "Contract JARVIS selected for this signal" : "AI-selected strike"}
                    >
                      {isSelectedPut ? "SELECTED" : "AI"}
                    </span>
                  )}
                </td>
                <td className="chain-td" style={{ color: "var(--muted)" }}>{row.put?.bid ?? "—"}</td>
                <td className="chain-td" style={{ color: "var(--muted)" }}>{row.put?.ask ?? "—"}</td>
                <td className="chain-td">{row.put?.volume?.toLocaleString() ?? "—"}</td>
                <td className="chain-td" style={{ color: (row.put?.change_in_oi ?? 0) >= 0 ? "var(--green)" : "var(--red)" }}>
                  {row.put?.change_in_oi?.toLocaleString() ?? "—"}
                </td>
                <td className="chain-td" style={{ background: putItm && !isAtm ? "color-mix(in srgb, var(--red) 6%, transparent)" : undefined }}>
                  <OiCell value={row.put?.open_interest} max={maxOi} side="put" highlight={isHighestOi(row.put?.open_interest)} />
                </td>
                <td className="chain-td" style={{ color: "var(--muted)" }}>{row.put?.iv ?? "—"}</td>
                <td className="chain-td" style={{ color: "var(--muted)" }}>
                  {row.put?.greeks?.delta != null ? row.put.greeks.delta.toFixed(2) : "—"}
                </td>
                <td className="chain-td" style={{ color: "var(--muted)" }}>
                  {row.put?.greeks?.gamma != null ? row.put.greeks.gamma.toFixed(4) : "—"}
                </td>
                <td className="chain-td" style={{ color: "var(--muted)" }}>
                  {row.put?.greeks?.theta != null ? row.put.greeks.theta.toFixed(2) : "—"}
                </td>
                <td className="chain-td" style={{ color: "var(--muted)" }}>
                  {row.put?.greeks?.vega != null ? row.put.greeks.vega.toFixed(2) : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
