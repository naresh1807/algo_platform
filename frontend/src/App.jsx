import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import Sidebar from "./components/Sidebar.jsx";
import TopBar from "./components/TopBar.jsx";
import JarvisPanel from "./components/JarvisPanel.jsx";
import SignalAlertPopup from "./components/SignalAlertPopup.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Learning from "./pages/Learning.jsx";
import Login from "./pages/Login.jsx";
import News from "./pages/News.jsx";
import OptionsAnalytics from "./pages/OptionsAnalytics.jsx";
import StockInvesting from "./pages/StockInvesting.jsx";
import Positions from "./pages/Positions.jsx";
import RiskLog from "./pages/RiskLog.jsx";
import Signals from "./pages/Signals.jsx";
import { useLiveStore } from "./store/liveStore.js";

/**
 * Wraps every route except /login -- redirects to /login if there's no
 * stored token. This is what was missing: without this guard AND the
 * Login page, the app rendered the dashboard shell regardless of auth
 * state, made API calls with no token, and every one of them 403'd
 * (exactly what the screenshots showed).
 */
function RequireAuth({ children }) {
  const location = useLocation();
  const token = localStorage.getItem("authToken");
  if (!token) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return children;
}

function AppShell() {
  // Connected exactly once here, for the lifetime of the authenticated
  // session -- not per-page. TopBar (rendered on every page, below)
  // reads killSwitchActive/connected/feedHealthy regardless of which
  // route is active, so the live sockets need to exist independently of
  // which page happens to be mounted. liveStore.connect() itself is
  // re-entrancy-guarded, so this effect re-running (e.g. React strict
  // mode's double-invoke in dev) is harmless.
  useEffect(() => {
    useLiveStore.getState().connect();
  }, []);

  return (
    <div className="app-shell">
      <TopBar />
      <div className="main-layout">
        <Sidebar />
        <main className="content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/signals" element={<Signals />} />
            <Route path="/news" element={<News />} />
            <Route path="/options" element={<OptionsAnalytics />} />
            <Route path="/investing" element={<StockInvesting />} />
            <Route path="/positions" element={<Positions />} />
            <Route path="/risk" element={<RiskLog />} />
            <Route path="/learning" element={<Learning />} />
          </Routes>
        </main>
        <aside className="right-panel">
          <div className="panel">
            <h3>Quick Notes</h3>
            <p style={{ fontSize: 12, color: "var(--muted)" }}>
              This right panel is reserved for whatever ends up being most
              useful to glance at constantly once real data is flowing --
              e.g. a compact watchlist or the day's P&amp;L ticker. Left
              empty for now rather than filled with placeholder content.
            </p>
          </div>
        </aside>
      </div>
      <SignalAlertPopup />
      <JarvisPanel />
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/*"
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      />
    </Routes>
  );
}
