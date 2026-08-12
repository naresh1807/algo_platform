import { useLiveStore } from "../store/liveStore.js";
import { useThemeStore } from "../store/themeStore.js";

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
