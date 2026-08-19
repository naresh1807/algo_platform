import { lazy, Suspense, useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import Sidebar from "./components/Sidebar.jsx";
import TopBar from "./components/TopBar.jsx";
import JarvisPanel from "./components/JarvisPanel.jsx";
import LoadingSkeleton from "./components/LoadingSkeleton.jsx";
import SignalAlertPopup from "./components/SignalAlertPopup.jsx";
import { useJarvisStore } from "./store/jarvisStore.js";
import { useLiveStore } from "./store/liveStore.js";
import { useUiStore } from "./store/uiStore.js";
import { AUTH_SESSION_CHANGED_EVENT } from "./utils/websocket.js";

const Dashboard = lazy(() => import("./pages/Dashboard.jsx"));
const JarvisSignalTerminal = lazy(() => import("./pages/JarvisSignalTerminal.jsx"));
const Learning = lazy(() => import("./pages/Learning.jsx"));
const Login = lazy(() => import("./pages/Login.jsx"));
const News = lazy(() => import("./pages/News.jsx"));
const OptionsAnalytics = lazy(() => import("./pages/OptionsAnalytics.jsx"));
const StockInvesting = lazy(() => import("./pages/StockInvesting.jsx"));
const Positions = lazy(() => import("./pages/Positions.jsx"));
const Reports = lazy(() => import("./pages/Reports.jsx"));
const RiskLog = lazy(() => import("./pages/RiskLog.jsx"));
const Scalping = lazy(() => import("./pages/Scalping.jsx"));
const Settings = lazy(() => import("./pages/Settings.jsx"));
const Signals = lazy(() => import("./pages/Signals.jsx"));

function RouteFallback() {
  return <LoadingSkeleton rows={6} />;
}

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

    const handleSessionChange = () => {
      useLiveStore.getState().disconnect();
      useJarvisStore.getState().disconnectAnnouncements();
      // A token swap can mean a different account. Reloading prevents
      // any prior account's Zustand state from remaining on screen.
      if (localStorage.getItem("authToken")) {
        window.location.reload();
      } else if (window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    };
    window.addEventListener(AUTH_SESSION_CHANGED_EVENT, handleSessionChange);
    return () => {
      window.removeEventListener(AUTH_SESSION_CHANGED_EVENT, handleSessionChange);
      useLiveStore.getState().disconnect();
      useJarvisStore.getState().disconnectAnnouncements();
    };
  }, []);

  const sidebarCollapsed = useUiStore((s) => s.sidebarCollapsed);

  return (
    <div className="app-shell">
      <TopBar />
      <div className={`main-layout${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
        <Sidebar />
        <main className="content">
          <Suspense fallback={<RouteFallback />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/jarvis-signals" element={<JarvisSignalTerminal />} />
              <Route path="/signals" element={<Signals />} />
              <Route path="/news" element={<News />} />
              <Route path="/options" element={<OptionsAnalytics />} />
              <Route path="/investing" element={<StockInvesting />} />
              <Route path="/positions" element={<Positions />} />
              <Route path="/scalping" element={<Scalping />} />
              <Route path="/reports" element={<Reports />} />
              <Route path="/risk" element={<RiskLog />} />
              <Route path="/learning" element={<Learning />} />
              <Route path="/settings" element={<Settings />} />
            </Routes>
          </Suspense>
        </main>
      </div>
      <SignalAlertPopup />
      <JarvisPanel />
    </div>
  );
}

export default function App() {
  return (
    <Suspense fallback={<RouteFallback />}>
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
    </Suspense>
  );
}
