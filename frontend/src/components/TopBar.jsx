import { useEffect, useRef, useState } from "react";
import { AlertOctagon, LineChart, Menu } from "lucide-react";

import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";
import { useUiStore } from "../store/uiStore.js";
import ConnectionStatus from "./ConnectionStatus.jsx";
import KillSwitchButton from "./KillSwitchButton.jsx";
import MarketStatus from "./MarketStatus.jsx";
import NotificationBell from "./NotificationBell.jsx";
import ThemeToggle from "./ThemeToggle.jsx";
import TradingModeSwitch from "./TradingModeSwitch.jsx";
import UserMenu from "./UserMenu.jsx";

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
    <span style={{ fontSize: 12.5, color: "var(--muted)", whiteSpace: "nowrap" }}>
      VIX <strong style={{ color: "var(--text)" }}>{Number(vix.ltp).toFixed(2)}</strong>
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
 * Today's P&L -- real data from apps.risk.AccountEquity (same endpoint
 * RiskLog.jsx already reads), refreshed on an interval rather than a
 * live push since liveStore's risk_live socket only carries kill-switch
 * flips/feed-health/risk-alert events, not a per-tick P&L stream.
 */
function TodaysPnlBadge() {
  const [equity, setEquity] = useState(null);

  useEffect(() => {
    const load = () => endpoints.equity().then((res) => setEquity(res.data)).catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  if (!equity || equity.daily_pnl_pct === undefined) return null;
  const pct = equity.daily_pnl_pct;
  const positive = pct >= 0;

  return (
    <span title="Today's P&L (% of day's starting equity)" style={{ fontSize: 12.5, whiteSpace: "nowrap" }}>
      <span style={{ color: "var(--muted)" }}>Today </span>
      <strong style={{ color: positive ? "var(--green)" : "var(--red)" }}>
        {positive ? "+" : ""}
        {pct.toFixed(2)}%
      </strong>
    </span>
  );
}

/**
 * The kill-switch banner renders at the very top, above everything
 * else, full-width, so it can never be scrolled past or missed --
 * per the manual, this is the single most important UI element in the
 * whole app.
 */
export default function TopBar() {
  const killSwitchActive = useLiveStore((s) => s.killSwitchActive);
  const sidebarCollapsed = useUiStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);

  return (
    <>
      {killSwitchActive && (
        <div className="kill-switch-banner">
          <AlertOctagon size={14} style={{ verticalAlign: -2, marginRight: 6 }} />
          KILL SWITCH ACTIVE — all entries halted. Re-arming requires a manual backend action.
        </div>
      )}
      <header className="topbar">
        <div className="topbar-section">
          <button
            type="button"
            className="icon-btn"
            onClick={toggleSidebar}
            aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            <Menu size={16} />
          </button>
          <span className="brand">
            <span className="brand-mark">
              <LineChart size={16} />
            </span>
            <span className="brand-text">Algo Trading Platform</span>
          </span>
          <div className="topbar-divider" />
          <MarketStatus />
          <IndiaVixBadge />
        </div>

        <div className="topbar-section">
          <ConnectionStatus />
          <div className="topbar-divider" />
          <TradingModeSwitch />
          <TodaysPnlBadge />
          <div className="topbar-divider" />
          <NotificationBell />
          <ThemeToggle />
          <KillSwitchButton />
          <UserMenu />
        </div>
      </header>
    </>
  );
}
