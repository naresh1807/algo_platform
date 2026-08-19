import { useEffect, useRef, useState } from "react";

import { endpoints } from "../services/api.js";
import { createAuthenticatedWebSocket } from "../utils/websocket.js";

const MAX_RETAINED_CANDLES = 2000;
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;

function wsBase() {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}`;
}

// Same-side nearest-strike fallback, used only when the exact requested
// strike doesn't exist on the current expiry (typically right after a
// rollover) -- `chainRows` is the page's own already-loaded option
// chain for the CURRENT expiry, so this never guesses a strike the
// backend hasn't actually listed.
function findNearestStrike(chainRows, targetStrike, side) {
  const key = side === "CE" ? "call" : "put";
  const candidates = chainRows.filter((r) => r[key]?.ltp != null);
  if (candidates.length === 0) return null;
  return candidates.reduce((best, r) =>
    Math.abs(r.strike - targetStrike) < Math.abs(best.strike - targetStrike) ? r : best
  ).strike;
}

/**
 * Resolves + streams ONE option contract's own OHLCV candles, addressed
 * by {underlying, expiry, strike, optionType, timeframe} -- the backend
 * (apps.options.candle_service.resolve_contract, via
 * endpoints.optionCandles) is what actually turns that identity into a
 * real contract/token, never this hook.
 *
 * Returns `candles` (REST history, a stable array reference that only
 * changes on a real refetch) and `liveCandle` (the single latest WS
 * bar, changing on every tick) as SEPARATE values -- same split
 * PriceChart.jsx already uses for the underlying's own chart, so the
 * chart component can setData() only on a real contract/timeframe
 * switch and update() cheaply on every tick, instead of re-running
 * setData() on the whole array every time a tick arrives.
 *
 * Every identity field changing (including `expiry` changing out from
 * under an already-open chart on a rollover) tears down the previous
 * WS subscription, clears `candles`/`liveCandle`, and starts a fresh
 * fetch. A monotonic requestId guards against a slow response for a
 * previous selection ever overwriting a newer one. If the exact
 * strike/side 404s on `expiry` (rolled over, no longer listed), this
 * hook retries once against the nearest same-side strike from
 * `chainRows` before giving up with a real error.
 */
export function useOptionCandles({ underlying, expiry, strike, optionType, timeframe, chainRows = [] }) {
  const [contract, setContract] = useState(null);
  const [candles, setCandles] = useState([]);
  const [liveCandle, setLiveCandle] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [stale, setStale] = useState(false);

  const requestIdRef = useRef(0);
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const chainRowsRef = useRef(chainRows);
  chainRowsRef.current = chainRows;

  const active = Boolean(underlying && expiry && strike != null && optionType && timeframe);

  useEffect(() => {
    const requestId = ++requestIdRef.current;

    setContract(null);
    setCandles([]);
    setLiveCandle(null);
    setError(null);

    if (!active) {
      setLoading(false);
      return;
    }
    setLoading(true);

    const applyResponse = (res) => {
      if (requestIdRef.current !== requestId) return; // superseded by a newer selection
      setContract(res.data.contract);
      const sorted = [...res.data.candles].sort((a, b) => new Date(a.time) - new Date(b.time));
      setCandles(sorted.slice(-MAX_RETAINED_CANDLES));
      setLoading(false);
    };

    endpoints
      .optionCandles({ underlying, expiry, strike, option_type: optionType, timeframe })
      .then(applyResponse)
      .catch((err) => {
        if (requestIdRef.current !== requestId) return;

        if (err.response?.status === 404) {
          const nearest = findNearestStrike(chainRowsRef.current, strike, optionType);
          if (nearest != null && nearest !== strike) {
            endpoints
              .optionCandles({ underlying, expiry, strike: nearest, option_type: optionType, timeframe })
              .then(applyResponse)
              .catch(() => {
                if (requestIdRef.current !== requestId) return;
                setError("This contract is no longer available.");
                setLoading(false);
              });
            return;
          }
          setError("This contract is no longer available.");
        } else {
          setError(
            err.code === "ECONNABORTED"
              ? "The request timed out. The backend may be under load -- try again."
              : "Could not load this contract's candles."
          );
        }
        setLoading(false);
      });
    // chainRows deliberately excluded from deps -- it's read via
    // chainRowsRef only inside the 404 fallback above; depending on it
    // directly would re-trigger this whole fetch on every chain tick,
    // exactly the "don't recreate/refetch on every tick" behavior this
    // hook must avoid.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [underlying, expiry, strike, optionType, timeframe, active]);

  // WebSocket: one connection per resolved contract+timeframe, torn
  // down and reopened on every change -- never reused across a switch,
  // so a stale subscription can never keep delivering ticks for a
  // contract the user has since navigated away from.
  useEffect(() => {
    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.close();
      wsRef.current = null;
    }
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    setStale(false);
    if (!contract?.id || !timeframe) return undefined;

    let cancelled = false;
    const openSocket = (attempt = 0) => {
      if (cancelled) return;
      const socket = createAuthenticatedWebSocket(
        `${wsBase()}/ws/options/candles/${contract.id}/${timeframe}/`
      );
      if (!socket) {
        setStale(true);
        return;
      }
      wsRef.current = socket;
      socket.onopen = () => setStale(false);
      socket.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        setLiveCandle((prev) => {
          // Out-of-order guard: a briefly-delayed tick arriving after a
          // newer one must never move the chart's live bar backwards.
          if (prev && new Date(msg.timestamp).getTime() < new Date(prev.timestamp).getTime()) return prev;
          return msg;
        });
      };
      socket.onclose = () => {
        if (cancelled) return;
        setStale(true);
        const delay = Math.min(RECONNECT_MAX_DELAY_MS, RECONNECT_BASE_DELAY_MS * 2 ** attempt);
        reconnectTimerRef.current = setTimeout(() => openSocket(attempt + 1), delay);
      };
      socket.onerror = () => socket.close();
    };
    openSocket();

    return () => {
      cancelled = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [contract?.id, timeframe]);

  return { contract, candles, liveCandle, loading, error, stale };
}
