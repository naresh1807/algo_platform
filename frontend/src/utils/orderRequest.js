// getRandomValues also works when a LAN dashboard uses plain HTTP.
export function newOrderRequestId() {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const PENDING_KEY = "optionTradingPendingRequest";
export function readPendingOrder() {
  try {
    const order = JSON.parse(sessionStorage.getItem(PENDING_KEY) || "null");
    return order?.request_id && ["buy", "sell"].includes(order.action) ? order : null;
  } catch { return null; }
}
export function rememberPendingOrder(order) {
  try {
    if (order) sessionStorage.setItem(PENDING_KEY, JSON.stringify(order));
    else sessionStorage.removeItem(PENDING_KEY);
  } catch { /* The in-memory request still protects retries if storage is unavailable. */ }
}

