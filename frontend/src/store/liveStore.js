import { create } from "zustand";

/**
 * Holds everything that changes via WebSocket push (manual section 10:
 * live candles, signal updates, P&L, risk alerts, kill-switch status,
 * feed health) plus the connect() function that wires the sockets up.
 *
 * Kept as one store (not one-hook-per-socket) because the dashboard
 * needs to render several of these together consistently (e.g. a P&L
 * update and a risk alert arriving in the same tick) -- one store makes
 * that a single React re-render instead of several uncoordinated ones.
 *
 * Every socket here has a real backend consumer now (apps/market_data,
 * apps/signals, apps/risk, apps/options, apps/investing each ship their
 * own consumers.py/routing.py, combined in config/asgi.py) -- still
 * written defensively (auto-reconnect with backoff) since a Channels
 * worker restart or network blip is a normal thing to recover from,
 * not evidence anything is unimplemented.
 *
 * connect() is called exactly once, from AppShell (App.jsx) on mount --
 * NOT per-page. It previously lived in both Dashboard.jsx and
 * OptionsAnalytics.jsx's own effects with no re-entrancy guard, so
 * navigating Dashboard -> Options Analytics -> Dashboard opened a fresh
 * set of 5 sockets each time without ever closing the old ones (a real
 * connection leak + duplicated pushed messages over a session). The
 * `_generation` guard below still protects against any other future
 * caller doing the same thing, same "safe to call twice" spirit as
 * jarvisStore.connectAnnouncements's own `if (get().connected) return`.
 */
const SOCKET_DEFS = [
  { key: "marketData", path: "/ws/market-data/live/", handle: (set) => (msg) => set({ latestCandle: msg }) },
  { key: "signals", path: "/ws/signals/live/", handle: (set) => (msg) => set({ latestSignal: msg }) },
  { key: "risk", path: "/ws/risk/live/", handle: handleRiskMessage },
  { key: "options", path: "/ws/options/live/", handle: (set) => (msg) => set({ latestOptionUpdate: msg }) },
  { key: "investing", path: "/ws/investing/index-live/", handle: (set) => (msg) => set({ latestIndexUpdate: msg }) },
];

export const useLiveStore = create((set, get) => ({
  killSwitchActive: false,
  latestCandle: null,
  latestSignal: null,
  latestOptionUpdate: null,
  latestIndexUpdate: null,
  positionsPnl: {},
  riskAlerts: [],
  feedHealthy: true,

  // `connected` is true only once EVERY socket is open, and flips false
  // the moment any one of them drops -- a single shared boolean written
  // by all 5 sockets' onopen/onclose used to make this misreport
  // "connected" with only 1 of 5 up, or "disconnected" when just one of
  // five dropped while the rest were healthy. `socketStatus` exposes
  // the per-socket detail for anything that wants it (e.g. a future
  // "which feed is down" indicator); `connected` stays the simple
  // all-or-nothing summary TopBar already reads today.
  socketStatus: Object.fromEntries(SOCKET_DEFS.map((d) => [d.key, false])),
  connected: false,

  _generation: 0,
  _sockets: {},

  connect: () => {
    if (get()._generation > 0) return; // already connected/connecting -- see module docstring
    const generation = get()._generation + 1;
    set({ _generation: generation });

    SOCKET_DEFS.forEach(({ key, path, handle }) => {
      openWithRetry(key, `${wsBase()}${path}`, handle(set), set, get, generation);
    });
  },

  // Closes every tracked socket and invalidates any pending reconnect
  // timers (via the generation guard in openWithRetry) so a stale timer
  // from a previous connect() can never resurrect a socket after
  // disconnect() -- not called anywhere today (AppShell stays mounted
  // for the whole authenticated session), but provided so any future
  // caller (e.g. on logout) has a real, complete teardown instead of
  // reimplementing one.
  disconnect: () => {
    const { _sockets } = get();
    Object.values(_sockets).forEach((socket) => {
      socket.onclose = null; // don't let the intentional close trigger a reconnect
      socket.close();
    });
    set({
      _generation: 0,
      _sockets: {},
      connected: false,
      socketStatus: Object.fromEntries(SOCKET_DEFS.map((d) => [d.key, false])),
    });
  },
}));

function wsBase() {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}`;
}

function setSocketOpen(key, isOpen, set, get) {
  const socketStatus = { ...get().socketStatus, [key]: isOpen };
  const connected = Object.values(socketStatus).every(Boolean);
  set({ socketStatus, connected });
}

function openWithRetry(key, url, onMessage, set, get, generation, attempt = 0) {
  // A stale reconnect timer from a superseded connect()/disconnect()
  // cycle must not open a new socket -- compare against the current
  // generation before doing anything.
  if (get()._generation !== generation) return;

  const socket = new WebSocket(url);
  set((state) => ({ _sockets: { ...state._sockets, [key]: socket } }));

  socket.onopen = () => setSocketOpen(key, true, set, get);
  socket.onmessage = (event) => onMessage(JSON.parse(event.data));
  socket.onclose = () => {
    setSocketOpen(key, false, set, get);
    if (get()._generation !== generation) return; // disconnect() already tore this down
    // Exponential backoff, capped at 30s, so a backend restart doesn't
    // get hammered with reconnect attempts.
    const delay = Math.min(30000, 1000 * 2 ** attempt);
    setTimeout(() => openWithRetry(key, url, onMessage, set, get, generation, attempt + 1), delay);
  };
  socket.onerror = () => socket.close();
}

function handleRiskMessage(set) {
  return (msg) => {
    if (msg.type === "kill_switch") {
      set({ killSwitchActive: msg.is_active });
    } else if (msg.type === "feed_health") {
      set({ feedHealthy: msg.is_healthy });
    } else {
      set((state) => ({ riskAlerts: [msg, ...state.riskAlerts].slice(0, 50) }));
    }
  };
}
