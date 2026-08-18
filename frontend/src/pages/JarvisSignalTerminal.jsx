import { useEffect, useMemo, useRef, useState } from "react";

import DecisionFactors from "../components/DecisionFactors.jsx";
import OptionChainTable from "../components/OptionChainTable.jsx";
import OptionContractChartModal from "../components/OptionContractChartModal.jsx";
import SignalCard from "../components/SignalCard.jsx";
import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";

const selectStyle = {
  background: "var(--panel)", color: "var(--text)",
  border: "1px solid var(--border)", borderRadius: 6, padding: 6,
};

/**
 * JARVIS AI Signal Terminal -- the professional trading-terminal view
 * the redesign asked for: underlying analysis -> option chain -> exact
 * resolved contract -> risk-validated JARVIS signal, all in one place,
 * instead of a bare "NIFTY -> BUY" line. Deliberately built by
 * REUSING the pieces that already exist rather than duplicating them:
 *   - Option chain rendering: components/OptionChainTable.jsx (the same
 *     component OptionsAnalytics.jsx now uses, extracted from there)
 *   - Live chain updates: useLiveStore's latestOptionUpdate, same
 *     REST-loads-history / WebSocket-keeps-it-fresh pattern
 *     OptionsAnalytics.jsx already established
 *   - Live signal push: useLiveStore's latestSignal (apps.signals.
 *     signals.py's broadcast, now carrying option_contract_id/
 *     ml_win_probability/options_score/rejection_stage too)
 *   - Signal rendering: components/SignalCard.jsx + DecisionFactors.jsx
 *   - Positions/execution-mode: the same REST endpoints Positions.jsx
 *     and Settings.jsx already call
 *
 * The pipeline itself (apps.options.index_direction_strategy) already
 * runs on the existing 5-minute Celery beat schedule -- this page reads
 * its output (REST + live push) and offers a manual "Re-analyze Now"
 * button (apps.options.views.EvaluateNowView) rather than re-running
 * any trading logic client-side.
 */
export default function JarvisSignalTerminal() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [signal, setSignal] = useState(null);
  const [signalHistory, setSignalHistory] = useState([]);
  const [evaluating, setEvaluating] = useState(false);
  const [evaluateError, setEvaluateError] = useState(null);

  const [expiries, setExpiries] = useState([]);
  const [expiry, setExpiry] = useState("");
  const [chainRows, setChainRows] = useState([]);
  const [spot, setSpot] = useState(null);
  const chainContainerRef = useRef(null);
  const atmRowRef = useRef(null);
  const [selectedContract, setSelectedContract] = useState(null);
  const [flash, setFlash] = useState({});
  const prevLtpRef = useRef({});

  const [positions, setPositions] = useState([]);
  const [executionMode, setExecutionModeState] = useState(null);
  const [strategySettings, setStrategySettings] = useState(null);

  const latestSignal = useLiveStore((s) => s.latestSignal);
  const latestOptionUpdate = useLiveStore((s) => s.latestOptionUpdate);

  // Live sockets are connected once at the AppShell level (App.jsx) --
  // see liveStore.js's module docstring.

  const fetchLatestSignal = () => {
    endpoints.signals({ symbol: underlying, page_size: 1 }).then((res) => {
      setSignal(res.data.results?.[0] ?? null);
    });
  };

  useEffect(() => {
    fetchLatestSignal();
    endpoints.signals({ symbol: underlying, page_size: 20 }).then((res) => {
      setSignalHistory(res.data.results ?? []);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [underlying]);

  // A newly-pushed signal for THIS underlying triggers a fresh REST
  // fetch (for the fuller object, incl. option_contract_detail, that
  // the deliberately-lean WebSocket payload doesn't carry -- see
  // apps.signals.signals.py's broadcast docstring) instead of just
  // merging the live push in directly.
  const lastSeenSignalIdRef = useRef(null);
  useEffect(() => {
    if (!latestSignal || latestSignal.symbol !== underlying) return;
    if (latestSignal.id === lastSeenSignalIdRef.current) return;
    lastSeenSignalIdRef.current = latestSignal.id;
    fetchLatestSignal();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [latestSignal, underlying]);

  useEffect(() => {
    endpoints.positions().then((res) => setPositions(res.data.results ?? []));
    endpoints.executionMode().then((res) => setExecutionModeState(res.data));
    endpoints.optionsStrategySettings().then((res) => setStrategySettings(res.data));
  }, []);

  // Option chain: same load-expiries -> load-chain pattern as
  // OptionsAnalytics.jsx (see that page for the race-condition-guard
  // reasoning behind the `cancelled` flags below).
  useEffect(() => {
    let cancelled = false;
    setExpiry("");
    setChainRows([]);
    endpoints.optionExpiries(underlying).then((res) => {
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
    endpoints.optionChain(underlying, expiry).then((res) => {
      if (cancelled) return;
      setChainRows(res.data.rows ?? []);
      setSpot(res.data.spot ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [underlying, expiry]);

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
          if (f[flashKey] !== direction) return f;
          const { [flashKey]: _drop, ...rest } = f;
          return rest;
        });
      }, 650);
    }
    prevLtpRef.current[flashKey] = latestOptionUpdate.ltp;

    setChainRows((prev) => {
      const idx = prev.findIndex((r) => r.strike === latestOptionUpdate.strike);
      const updatedLeg = {
        ltp: latestOptionUpdate.ltp, open_interest: latestOptionUpdate.open_interest,
        change_in_oi: latestOptionUpdate.change_in_oi, volume: latestOptionUpdate.volume,
        iv: latestOptionUpdate.iv, bid: latestOptionUpdate.bid, ask: latestOptionUpdate.ask,
        timestamp: latestOptionUpdate.timestamp, greeks: latestOptionUpdate.greeks,
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

  const atmStrike = useMemo(() => {
    if (spot == null || chainRows.length === 0) return null;
    return chainRows.reduce(
      (closest, row) => (Math.abs(row.strike - spot) < Math.abs(closest - spot) ? row.strike : closest),
      chainRows[0].strike
    );
  }, [chainRows, spot]);

  const signalStrike = signal?.strike_price != null ? Number(signal.strike_price) : null;
  const matchingChainRow = useMemo(
    () => (signalStrike != null ? chainRows.find((r) => r.strike === signalStrike) : null),
    [chainRows, signalStrike]
  );

  const underlyingPositions = positions.filter((p) => p.symbol?.startsWith(underlying));

  const handleEvaluateNow = () => {
    setEvaluating(true);
    setEvaluateError(null);
    endpoints.evaluateNow(underlying).then((res) => {
      setSignal(res.data);
      setEvaluating(false);
    }).catch((err) => {
      setEvaluateError(err.response?.data?.error || "Re-analysis failed.");
      setEvaluating(false);
    });
  };

  const handleStrategySettingChange = (field, value) => {
    const next = { ...strategySettings, [field]: value };
    setStrategySettings(next);
    endpoints.setOptionsStrategySettings({ [field]: value }).then((res) => setStrategySettings(res.data));
  };

  return (
    <div>
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 style={{ margin: 0 }}>
            Market Overview — {underlying}
            {spot != null && (
              <span style={{ fontSize: 13, fontWeight: 400, color: "var(--muted)", marginLeft: 10 }}>
                Spot {spot.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                {atmStrike != null && ` · ATM ${atmStrike}`}
              </span>
            )}
          </h3>
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <select value={underlying} onChange={(e) => setUnderlying(e.target.value)} style={selectStyle}>
              <option value="NIFTY">NIFTY</option>
              <option value="BANKNIFTY">BANKNIFTY</option>
            </select>
            <button
              onClick={handleEvaluateNow}
              disabled={evaluating}
              style={{
                padding: "6px 12px", borderRadius: 6, cursor: evaluating ? "default" : "pointer",
                border: "1px solid var(--accent)", background: "transparent", color: "var(--accent)",
              }}
            >
              {evaluating ? "Analyzing…" : "Re-analyze Now"}
            </button>
            {executionMode && (
              <span className={`badge ${executionMode.mode === "live" ? "badge-red" : "badge-green"}`}>
                {executionMode.mode === "live" ? "LIVE" : "PAPER"} TRADING
              </span>
            )}
          </div>
        </div>
        {evaluateError && <p style={{ color: "var(--red)", fontSize: 12, marginTop: 8 }}>{evaluateError}</p>}

        {strategySettings && (
          <div style={{ display: "flex", gap: 16, marginTop: 12, fontSize: 12, color: "var(--muted)", flexWrap: "wrap" }}>
            <label>
              Expiry mode:{" "}
              <select
                value={strategySettings.expiry_mode}
                onChange={(e) => handleStrategySettingChange("expiry_mode", e.target.value)}
                style={{ ...selectStyle, padding: 3, fontSize: 12 }}
              >
                <option value="current_week">Current Week</option>
                <option value="next_week">Next Week</option>
                <option value="monthly">Monthly</option>
                <option value="custom">Custom</option>
              </select>
            </label>
            <label>
              Strike mode:{" "}
              <select
                value={strategySettings.strike_mode}
                onChange={(e) => handleStrategySettingChange("strike_mode", e.target.value)}
                style={{ ...selectStyle, padding: 3, fontSize: 12 }}
              >
                <option value="atm">ATM</option>
                <option value="itm">ITM</option>
                <option value="otm">OTM</option>
                <option value="ai">AI Selected</option>
              </select>
            </label>
          </div>
        )}
      </div>

      <div className="two-col-grid" style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: 12 }}>
        <SignalCard signal={signal} />
        <DecisionFactors signal={signal} chainRow={matchingChainRow} />
      </div>

      <div className="panel">
        <h3>Option Chain — {underlying} {expiry && `· ${expiry}`}</h3>
        {expiries.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No expiries synced yet for {underlying}.</p>
        ) : (
          <OptionChainTable
            rows={chainRows}
            spot={spot}
            atmStrike={atmStrike}
            flash={flash}
            aiSelectedStrike={signalStrike}
            aiSelectedSide={signal?.option_side}
            selectedContractStrike={signal?.status === "approved" || signal?.status === "executed" ? signalStrike : null}
            selectedContractSide={signal?.status === "approved" || signal?.status === "executed" ? signal?.option_side : null}
            containerRef={chainContainerRef}
            atmRowRef={atmRowRef}
            onCellClick={(strike, optionType) => setSelectedContract({ strike, optionType, underlying, expiry })}
          />
        )}
      </div>

      <div className="two-col-grid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <div className="panel">
          <h3>Active Positions — {underlying}</h3>
          {underlyingPositions.length === 0 ? (
            <p style={{ color: "var(--muted)" }}>No open positions.</p>
          ) : (
            underlyingPositions.map((p) => (
              <div key={p.id} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderTop: "1px solid var(--border)", fontSize: 13 }}>
                <span>{p.symbol} · {p.side.toUpperCase()} x{p.qty}</span>
                <span style={{ color: Number(p.unrealized_pnl) >= 0 ? "var(--green)" : "var(--red)" }}>
                  {p.unrealized_pnl}
                </span>
              </div>
            ))
          )}
        </div>

        <div className="panel">
          <h3>Signal History — {underlying}</h3>
          {signalHistory.length === 0 ? (
            <p style={{ color: "var(--muted)" }}>No signals yet.</p>
          ) : (
            signalHistory.map((s) => (
              <div
                key={s.id}
                onClick={() => setSignal(s)}
                style={{ cursor: "pointer", padding: "6px 0", borderTop: "1px solid var(--border)", fontSize: 12 }}
              >
                <span style={{ color: s.status === "approved" || s.status === "executed" ? "var(--green)" : "var(--muted)" }}>
                  {s.status.toUpperCase()}
                </span>{" "}
                {s.strike_price != null && `${Number(s.strike_price)} ${s.option_side} `}
                <span style={{ color: "var(--muted)" }}>{new Date(s.created_at).toLocaleString()}</span>
              </div>
            ))
          )}
        </div>
      </div>

      <OptionContractChartModal contract={selectedContract} onClose={() => setSelectedContract(null)} />
    </div>
  );
}
