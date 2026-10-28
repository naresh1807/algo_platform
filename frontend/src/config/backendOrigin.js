/**
 * Where does this app find the Django backend?
 *
 * The plain browser build (npm run dev, or a production build served
 * from the same origin as Django / behind one reverse proxy) has never
 * needed this -- api.js's relative "/api" and liveStore.js's wsBase()
 * (derived from window.location) already work because the page and the
 * API share one origin. That assumption breaks the moment the exact
 * same build is loaded inside a wrapper shell instead of a browser tab:
 *
 *   - Electron (frontend/electron/main.js) serves the built dist/ from
 *     its own tiny local static server and, on desktop, launches the
 *     backend on that SAME machine -- so "same machine" is still a
 *     reasonable default there, even though it's no longer literally
 *     "same origin" (different port).
 *   - Capacitor (Android) serves dist/ from a synthetic
 *     https://localhost WebView origin that has nothing to do with
 *     wherever the backend actually runs (another machine on the LAN,
 *     or a cloud host) -- there is no correct default. The user
 *     configures it once via pages/ServerSettings.jsx.
 *
 * This module is the one place that decides "same-origin" vs
 * "configured elsewhere" so api.js and liveStore.js don't each grow
 * their own copy of that logic. Every function here is a pure read of
 * localStorage/window globals -- no network calls, safe to call from
 * module-load time.
 */

const STORAGE_KEY = "backendOrigin";

/** Set by frontend/electron/preload.js via contextBridge -- see that file. */
export function isElectron() {
  return typeof window !== "undefined" && Boolean(window.electronAPI?.isElectron);
}

/** Injected automatically by the Capacitor native runtime -- not something this app sets itself. */
export function isCapacitorNative() {
  return typeof window !== "undefined" && Boolean(window.Capacitor?.isNativePlatform?.());
}

export function isWrappedApp() {
  return isElectron() || isCapacitorNative();
}

/**
 * "" means same-origin -- the historical behavior, unchanged for the
 * plain browser build. Anything else is an absolute http(s)://host:port
 * to send REST/WebSocket traffic to instead (no trailing slash).
 */
export function getBackendOrigin() {
  if (!isWrappedApp()) return "";
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) return stored;
  // Electron controls the backend on the same machine (electron/main.js
  // launches it before creating the window) -- defaulting here means
  // most desktop users never see the Server Settings page at all.
  // Android has no equivalent guarantee, so it gets no default; see
  // RequireServerConfigured in App.jsx.
  return isElectron() ? "http://127.0.0.1:8000" : "";
}

export function setBackendOrigin(origin) {
  localStorage.setItem(STORAGE_KEY, origin.trim().replace(/\/+$/, ""));
}

export function clearBackendOrigin() {
  localStorage.removeItem(STORAGE_KEY);
}
