import { lazy, Suspense, useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import BottomNav from "./components/BottomNav.jsx";
import PageHeader from "./components/PageHeader.jsx";
import Sidebar from "./components/Sidebar.jsx";
import TopBar from "./components/TopBar.jsx";
import JarvisPanel from "./components/JarvisPanel.jsx";
import LoadingSkeleton from "./components/LoadingSkeleton.jsx";
import RouteErrorBoundary from "./components/RouteErrorBoundary.jsx";
import SignalAlertPopup from "./components/SignalAlertPopup.jsx";
import { getBackendOrigin, isWrappedApp } from "./config/backendOrigin.js";
import { useJarvisStore } from "./store/jarvisStore.js";
import { useLiveStore } from "./store/liveStore.js";
import { useUiStore } from "./store/uiStore.js";
import { AUTH_SESSION_CHANGED_EVENT } from "./utils/websocket.js";

const Dashboard = lazy(() => import("./pages/Dashboard.jsx"));
const JarvisSignalTerminal = lazy(() => import("./pages/JarvisSignalTerminal.jsx"));
const Learning = lazy(() => import("./pages/Learning.jsx"));
const Login = lazy(() => import("./pages/Login.jsx"));
const News = lazy(() => import("./pages/News.jsx"));
const OptionTrading = lazy(() => import("./pages/OptionTrading.jsx"));
const OptionsAnalytics = lazy(() => import("./pages/OptionsAnalytics.jsx"));
const PaperTradingDashboard = lazy(() => import("./pages/PaperTradingDashboard.jsx"));
const BTST = lazy(() => import("./pages/BTST.jsx"));
const DeliveryTrading = lazy(() => import("./pages/DeliveryTrading.jsx"));
const StockInvesting = lazy(() => import("./pages/StockInvesting.jsx"));
const Orders = lazy(() => import("./pages/Orders.jsx"));
const Positions = lazy(() => import("./pages/Positions.jsx"));
const Reports = lazy(() => import("./pages/Reports.jsx"));
const RiskLog = lazy(() => import("./pages/RiskLog.jsx"));
const Scalping = lazy(() => import("./pages/Scalping.jsx"));
const ServerSettings = lazy(() => import("./pages/ServerSettings.jsx"));
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

/**
 * The plain browser build never redirects here -- isWrappedApp() is
 * false and this is a no-op. Inside Electron/Capacitor, api.js and
 * liveStore.js can't do anything useful (not even show the Login page's
 * own "invalid credentials" error) without a backend address configured
 * first -- see config/backendOrigin.js and pages/ServerSettings.jsx.
 */
function RequireServerConfigured({ children }) {
  const location = useLocation();
  if (isWrappedApp() && !getBackendOrigin()) {
    return <Navigate to="/server-settings" state={{ from: location }} replace />;
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
  const { pathname } = useLocation();

  return (
    <div className="app-shell">
      <a className="skip-to-content" href="#main-content">Skip to content</a>
      <TopBar />
      <div className={`main-layout${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
        <Sidebar />
        <main className="content" id="main-content">
          <PageHeader />
          {/* key={pathname} resets the boundary's crashed state on every
              navigation -- see RouteErrorBoundary's own docstring for why. */}
          <RouteErrorBoundary key={pathname}>
            <Suspense fallback={<RouteFallback />}>
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/jarvis-signals" element={<JarvisSignalTerminal />} />
                <Route path="/signals" element={<Signals />} />
                <Route path="/news" element={<News />} />
                <Route path="/options" element={<OptionsAnalytics />} />
                <Route path="/option-trading" element={<OptionTrading />} />
              <Route path="/paper-trading" element={<PaperTradingDashboard />} />
                <Route path="/btst" element={<BTST />} />
                <Route path="/delivery" element={<DeliveryTrading />} />
                <Route path="/investing" element={<StockInvesting />} />
                <Route path="/positions" element={<Positions />} />
                <Route path="/orders" element={<Orders />} />
                <Route path="/scalping" element={<Scalping />} />
                <Route path="/reports" element={<Reports />} />
                <Route path="/risk" element={<RiskLog />} />
                <Route path="/learning" element={<Learning />} />
                <Route path="/settings" element={<Settings />} />
              </Routes>
            </Suspense>
          </RouteErrorBoundary>
        </main>
      </div>
      <SignalAlertPopup />
      <JarvisPanel />
      {/* Sidebar (above) and BottomNav are both always mounted -- index.css's
          @media (max-width: 700px) block is what actually switches
          between them, not JS/UA-sniffing, so there's no layout flash
          on resize and no dependency on Capacitor's isNativePlatform()
          (a phone-width browser tab gets the same bottom-nav experience
          a real trading app would give it). */}
      <BottomNav />
    </div>
  );
}

export default function App() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route path="/server-settings" element={<ServerSettings />} />
        <Route
          path="/login"
          element={
            <RequireServerConfigured>
              <Login />
            </RequireServerConfigured>
          }
        />
        <Route
          path="/*"
          element={
            <RequireServerConfigured>
              <RequireAuth>
                <AppShell />
              </RequireAuth>
            </RequireServerConfigured>
          }
        />
      </Routes>
    </Suspense>
  );
}
