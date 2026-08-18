import { useCallback, useEffect, useRef, useState } from "react";

import { endpoints } from "../services/api.js";

// Refetch cadence while the tab is visible -- cheap (one small backend
// query, apps.options.expiry_service reads already-synced local rows,
// no broker call) and frequent enough to notice a rollover shortly
// after it happens without the user needing to reload the page.
const POLL_INTERVAL_MS = 5 * 60 * 1000;

/**
 * The ONE shared piece of expiry-selection logic on the frontend --
 * JarvisSignalTerminal.jsx and OptionsAnalytics.jsx both used to
 * independently fetch /options/expiries/ and auto-select its first
 * entry, with no re-validation afterward (a component that had been
 * open since before a rollover would just keep using whatever expiry
 * it originally picked). This hook is what replaces both copies:
 * fetches apps.options.views.OptionExpiryStatusView (the backend's own
 * computed current_expiry/next_expiry/available_expiries -- the
 * frontend never guesses an expiry weekday or does its own date math),
 * keeps the selected expiry valid by construction, and refetches on a
 * timer and whenever the tab becomes visible again (a rollover can
 * happen while the tab was in the background).
 *
 * Returns { expiry, setExpiry, availableExpiries, currentExpiry,
 * nextExpiry, lastSuccessfulSync, rolloverRequired, loading, error,
 * unavailable, refetch }. `unavailable` is true only when the backend
 * itself reports no valid expiry AND can't recover (503) -- callers
 * should show a clear "contract sync temporarily unavailable" message
 * for that state, not an empty dropdown.
 */
export function useExpiryStatus(underlying) {
  const [status, setStatus] = useState(null); // raw OptionExpiryStatusView body
  const [expiry, setExpiryState] = useState(""); // the user/component's currently-selected expiry (ISO string)
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [unavailable, setUnavailable] = useState(false);

  // Tracks the expiry the USER explicitly picked (vs. the auto-selected
  // default) so a refetch doesn't stomp on a deliberate "next week"
  // selection as long as it's still in available_expiries -- only an
  // expiry that's fallen OUT of available_expiries gets replaced.
  const userPickedRef = useRef(false);
  const underlyingRef = useRef(underlying);

  const fetchStatus = useCallback(() => {
    setLoading(true);
    setError(null);
    endpoints
      .optionExpiryStatus(underlying)
      .then((res) => {
        const body = res.data;
        setStatus(body);
        setUnavailable(res.status === 503);

        const available = body.available_expiries ?? [];
        setExpiryState((prev) => {
          if (prev && userPickedRef.current && available.includes(prev)) {
            return prev; // still valid -- keep the user's deliberate choice
          }
          userPickedRef.current = false;
          return body.current_expiry ?? (available[0] ?? "");
        });
      })
      .catch(() => {
        setError("Could not load expiry information from the backend.");
      })
      .finally(() => setLoading(false));
  }, [underlying]);

  // Underlying change resets the "user picked" flag -- switching from
  // NIFTY to BANKNIFTY must always re-default to the new underlying's
  // own current_expiry, never carry over a raw date string that might
  // not even exist for the new underlying.
  useEffect(() => {
    if (underlyingRef.current !== underlying) {
      userPickedRef.current = false;
      underlyingRef.current = underlying;
    }
    fetchStatus();
  }, [underlying, fetchStatus]);

  // Poll on an interval, and refetch immediately whenever the tab
  // regains visibility -- a rollover (or a sync that just completed)
  // may have happened while this tab was backgrounded. Both handlers
  // call the SAME fetchStatus, and the interval is cleared/recreated
  // only when `underlying` changes (via fetchStatus's own identity),
  // not on every render, so this can't accumulate duplicate timers or
  // fire redundant requests.
  useEffect(() => {
    const interval = setInterval(fetchStatus, POLL_INTERVAL_MS);
    const onVisibility = () => {
      if (document.visibilityState === "visible") fetchStatus();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [fetchStatus]);

  const setExpiry = useCallback((value) => {
    userPickedRef.current = true;
    setExpiryState(value);
  }, []);

  return {
    expiry,
    setExpiry,
    availableExpiries: status?.available_expiries ?? [],
    currentExpiry: status?.current_expiry ?? null,
    nextExpiry: status?.next_expiry ?? null,
    lastSuccessfulSync: status?.last_successful_sync ?? null,
    rolloverRequired: status?.rollover_required ?? false,
    loading,
    error,
    unavailable,
    refetch: fetchStatus,
  };
}
