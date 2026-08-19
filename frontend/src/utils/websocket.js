export const AUTH_SESSION_CHANGED_EVENT = "auth-session-changed";

const authenticatedSockets = new Set();
let socketAccountToken = null;

/** Close every authenticated socket before an account/token transition. */
export function closeAllAuthenticatedWebSockets() {
  for (const socket of authenticatedSockets) {
    if (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN) {
      socket.close(1000, "Authentication changed");
    }
  }
  authenticatedSockets.clear();
  socketAccountToken = null;
}

function announceSessionChange() {
  window.dispatchEvent(new Event(AUTH_SESSION_CHANGED_EVENT));
}

export function storeAuthenticationToken(token) {
  closeAllAuthenticatedWebSockets();
  localStorage.setItem("authToken", token);
  announceSessionChange();
}

export function clearAuthenticationToken() {
  localStorage.removeItem("authToken");
  closeAllAuthenticatedWebSockets();
  announceSessionChange();
}

// `storage` fires in the other tabs, not the tab that made the change.
// Notify the mounted shell there as well so it can invalidate reconnect
// timers and reload/leave the authenticated UI for the new account state.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key !== "authToken") return;
    closeAllAuthenticatedWebSockets();
    announceSessionChange();
  });
}

/**
 * Opens a same-origin WebSocket authenticated with the existing DRF token.
 * The token travels in the WebSocket subprotocol header rather than the URL,
 * keeping it out of proxy access logs and browser history.
 */
export function createAuthenticatedWebSocket(url) {
  const token = localStorage.getItem("authToken");
  if (!token) return null;

  if (socketAccountToken !== null && socketAccountToken !== token) {
    closeAllAuthenticatedWebSockets();
  }
  socketAccountToken = token;

  const socket = new WebSocket(url, ["drf-token", token]);
  authenticatedSockets.add(socket);
  socket.addEventListener("close", () => authenticatedSockets.delete(socket), { once: true });
  return socket;
}
