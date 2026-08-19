import { useEffect, useRef, useState } from "react";

import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";

/**
 * Live spot price for one underlying index (NIFTY/BANKNIFTY/etc), kept
 * fresh via the same real /ws/investing/index-live/ push
 * IndexTickerBar.jsx and TopBar.jsx's IndiaVixBadge already consume --
 * NOT a new data source, just a shared way to subscribe to it by
 * symbol instead of each page re-deriving the same
 * "fetch /investing/indices/ once, remember this symbol's id, match
 * latestIndexUpdate.index_id against it" dance independently.
 *
 * Fixes a real bug: the option-chain pages (OptionsAnalytics.jsx,
 * JarvisSignalTerminal.jsx) used to set `spot` ONCE from the initial
 * REST /options/chain/ response and never again -- individual option
 * premiums in the chain updated live (via latestOptionUpdate), but the
 * underlying spot price silently went stale for the rest of the
 * session, which is exactly how a page can end up showing e.g. "24150"
 * long after the real index has moved to 24100.
 *
 * Returns ONLY the live-ticked value (null until the first tick for
 * `symbol` arrives, and reset to null whenever `symbol` itself
 * changes) -- deliberately does NOT try to absorb a caller-supplied
 * "initial" value internally, since that value typically arrives
 * asynchronously (e.g. after the option chain's own REST fetch
 * resolves) on a LATER render than the symbol-change effect below,
 * which would otherwise miss it. Callers combine the two themselves:
 * `const spot = useLiveIndexSpot(underlying) ?? restSpot;` -- always
 * correct on every render, no seeding-order race.
 */
export function useLiveIndexSpot(symbol) {
  const [ltp, setLtp] = useState(null);
  const indexIdRef = useRef(null);
  const latestIndexUpdate = useLiveStore((s) => s.latestIndexUpdate);

  useEffect(() => {
    setLtp(null);
    indexIdRef.current = null;
    if (!symbol) return;
    let cancelled = false;
    endpoints.indices().then((res) => {
      if (cancelled) return;
      const row = (res.data.results ?? []).find((idx) => idx.symbol === symbol);
      if (!row) return;
      indexIdRef.current = row.id;
      if (row.latest_price?.ltp != null) setLtp(row.latest_price.ltp);
    });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  useEffect(() => {
    if (!latestIndexUpdate || latestIndexUpdate.kind !== "index_price") return;
    if (latestIndexUpdate.index_id !== indexIdRef.current) return;
    if (latestIndexUpdate.ltp != null) setLtp(latestIndexUpdate.ltp);
  }, [latestIndexUpdate]);

  return ltp;
}
