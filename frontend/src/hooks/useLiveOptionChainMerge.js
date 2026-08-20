import { useEffect, useRef } from "react";

// Every field a live options_live push may carry -- see mergeOptionUpdateIntoRows's
// own docstring for why only keys ACTUALLY PRESENT on the incoming message are ever copied.
const MERGE_FIELDS = ["contract_id", "ltp", "open_interest", "change_in_oi", "volume", "iv", "bid", "ask", "timestamp", "greeks"];

/**
 * Pure reducer: merges one options_live push into an already-loaded
 * option-chain `rows` array. Exported (not just used internally) so it
 * can be unit-tested directly, with no React rendering/hook plumbing
 * needed -- see frontend test suite's useLiveOptionChainMerge test file.
 *
 * FIELD-LEVEL merge, not a whole-leg replace: apps/options/live_feed.py's
 * fast LTP path broadcasts immediately, before change_in_oi/iv/greeks are
 * known, and OMITS those keys entirely (not null) for exactly that reason
 * -- this only copies keys the incoming message actually carries, so
 * those columns keep showing whatever the last full snapshot said instead
 * of flashing to "—" between snapshots.
 *
 * Matches by contract_id when the update carries one (both the REST chain
 * response and every live update now include it) -- a stable identity
 * match, immune to a strike/side momentarily existing twice during a
 * rollover. Falls back to a strike+side match only if contract_id is
 * absent (defensive, for any older producer).
 */
export function mergeOptionUpdateIntoRows(rows, update) {
  const side = update.option_type === "CE" ? "call" : "put";

  const byContractId =
    update.contract_id != null ? rows.findIndex((r) => r[side]?.contract_id === update.contract_id) : -1;
  const idx = byContractId !== -1 ? byContractId : rows.findIndex((r) => r.strike === update.strike);

  const incoming = {};
  for (const key of MERGE_FIELDS) {
    if (update[key] !== undefined) incoming[key] = update[key];
  }

  if (idx === -1) {
    const row = { strike: update.strike, call: null, put: null, [side]: incoming };
    return [...rows, row].sort((a, b) => a.strike - b.strike);
  }
  const next = [...rows];
  next[idx] = { ...next[idx], [side]: { ...next[idx][side], ...incoming } };
  return next;
}

/**
 * Shared "merge a live options_live tick into an already-loaded option
 * chain" effect -- extracted from what used to be an identical copy in
 * both OptionsAnalytics.jsx and JarvisSignalTerminal.jsx. All the actual
 * merge logic lives in mergeOptionUpdateIntoRows above; this hook is
 * just the React-side wiring (the underlying/expiry guard, the flash-on-
 * change timer, and the effect dependency list).
 */
export function useLiveOptionChainMerge({ latestOptionUpdate, underlying, expiry, setChainRows, setFlash }) {
  const prevLtpRef = useRef({});

  useEffect(() => {
    if (!latestOptionUpdate) return;
    if (latestOptionUpdate.underlying !== underlying || latestOptionUpdate.expiry !== expiry) return;

    const side = latestOptionUpdate.option_type === "CE" ? "call" : "put";
    const flashKey = `${latestOptionUpdate.strike}-${side}`;
    const prevLtp = prevLtpRef.current[flashKey];
    if (prevLtp != null && latestOptionUpdate.ltp != null && latestOptionUpdate.ltp !== prevLtp) {
      const direction = latestOptionUpdate.ltp > prevLtp ? "up" : "down";
      setFlash((f) => ({ ...f, [flashKey]: direction }));
      setTimeout(() => {
        setFlash((f) => {
          if (f[flashKey] !== direction) return f; // a newer flash already replaced this one
          const { [flashKey]: _drop, ...rest } = f;
          return rest;
        });
      }, 650);
    }
    prevLtpRef.current[flashKey] = latestOptionUpdate.ltp;

    setChainRows((prev) => mergeOptionUpdateIntoRows(prev, latestOptionUpdate));
  }, [latestOptionUpdate, underlying, expiry, setChainRows, setFlash]);
}
