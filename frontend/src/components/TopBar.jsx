import { useEffect, useRef, useState } from "react";

import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";
import { useThemeStore } from "../store/themeStore.js";

/**
 * India VIX in the header, live -- apps.investing.Index/IndexPriceSnapshot
 * already tick this in real time (apps.market_data.broker_client.
 * SYMBOL_TOKENS["INDIAVIX"] joins the same Angel One WebSocket
 * subscription every other index card uses, see that dict's own
 * comment), pushed over the SAME /ws/investing/index-live/ connection
 * liveStore.js already keeps open globally -- no new socket, no new
 * poll, just reading `latestIndexUpdate` for this one index's id.
 * Renders nothing until the initial REST fetch resolves (no
 * layout-shifting placeholder for a value that's either there or not).
 */
function IndiaVixBadge() {
  const [vix, setVix] = useState(null); // {ltp, change, change_pct} | null
  const vixIndexIdRef = useRef(null);
  const latestIndexUpdate = useLiveStore((s) => s.latestIndexUpdate);

  useEffect(() => {
    endpoints.indices().then((res) => {
      const row = (res.data.results ?? []).find((idx) => idx.symbol === "INDIAVIX");
      if (!row) return;
      vixIndexIdRef.current = row.id;
      if (row.latest_price) setVix(row.latest_price);
    });
  }, []);

  useEffect(() => {
    if (!latestIndexUpdate || latestIndexUpdate.kind !== "index_price") return;
    if (latestIndexUpdate.index_id !== vixIndexIdRef.current) return;
    setVix({
      ltp: latestIndexUpdate.ltp, change: latestIndexUpdate.change, change_pct: latestIndexUpdate.change_pct,
    });
  }, [latestIndexUpdate]);

  if (!vix) return null;
  const up = vix.change_pct != null && vix.change_pct >= 0;
  return (
    <span>
      India VIX <strong>{Number(vix.ltp).toFixed(2)}</strong>
      {vix.change_pct != null && (
        <span style={{ color: up ? "var(--green)" : "var(--red)", marginLeft: 4 }}>
          ({up ? "+" : ""}
          {vix.change_pct.toFixed(2)}%)
        </span>
      )}
    </span>
  );
}

/**
 * The kill-switch banner here is the single most important UI element
 * in the whole app per the manual ("the dashboard should show both
 * opportunity and safety state very clearly") -- it renders at the very
 * top, above everything else, full-width, so it can never be scrolled
 * past or missed.
 */
export default function TopBar() {
  const killSwitchActive = useLiveStore((s) => s.killSwitchActive);
  const connected = useLiveStore((s) => s.connected);
  const feedHealthy = useLiveStore((s) => s.feedHealthy);
  const theme = useThemeStore((s) => s.theme);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);

  return (
    <>
      {killSwitchActive && (
        <div className="kill-switch-banner">
          🛑 KILL SWITCH ACTIVE — all entries halted. Re-arming requires a
          manual action on the backend.
        </div>
      )}
      <div className="topbar">
        <strong>Algo Trading Platform</strong>
        <div style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 13 }}>
          <IndiaVixBadge />
          <span>
            <span className={`status-dot ${connected ? "ok" : "bad"}`} />
            {connected ? "Live feed connected" : "Live feed disconnected"}
          </span>
          <span>
            <span className={`status-dot ${feedHealthy ? "ok" : "warn"}`} />
            Broker feed {feedHealthy ? "healthy" : "degraded"}
          </span>
          <button
            type="button"
            className="theme-toggle"
            onClick={toggleTheme}
            aria-label="Toggle dark/light mode"
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {theme === "dark" ? "🌙 Dark" : "☀️ Light"}
          </button>
        </div>
      </div>
    </>
  );
}
