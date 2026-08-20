import { describe, expect, it } from "vitest";

import { mergeOptionUpdateIntoRows } from "./useLiveOptionChainMerge.js";

describe("mergeOptionUpdateIntoRows", () => {
  it("merges a fast LTP-only update into the correct CE row without touching the PE row", () => {
    const rows = [
      {
        strike: 24500,
        call: { contract_id: 1, ltp: 100, open_interest: 5000, change_in_oi: 200, iv: 14.2 },
        put: { contract_id: 2, ltp: 80, open_interest: 4000, change_in_oi: -100, iv: 13.8 },
      },
    ];
    // A fast LTP-only push (apps/options/live_feed.py's fast path) --
    // change_in_oi/iv/greeks deliberately absent, not null.
    const update = { contract_id: 1, underlying: "NIFTY", expiry: "2026-08-27", strike: 24500, option_type: "CE", ltp: 101, open_interest: 5010, bid: 100.5, ask: 101.5, volume: 42, timestamp: "t1" };

    const next = mergeOptionUpdateIntoRows(rows, update);

    expect(next).toHaveLength(1);
    expect(next[0].call.ltp).toBe(101);
    expect(next[0].call.open_interest).toBe(5010);
    // Untouched by this update -- must survive from the previous full snapshot.
    expect(next[0].call.change_in_oi).toBe(200);
    expect(next[0].call.iv).toBe(14.2);
    // The PE leg must be completely unaffected.
    expect(next[0].put).toEqual(rows[0].put);
  });

  it("merges into the PE row when option_type is PE", () => {
    const rows = [{ strike: 24500, call: { contract_id: 1, ltp: 100 }, put: { contract_id: 2, ltp: 80 } }];
    const update = { contract_id: 2, underlying: "NIFTY", expiry: "2026-08-27", strike: 24500, option_type: "PE", ltp: 82 };

    const next = mergeOptionUpdateIntoRows(rows, update);

    expect(next[0].put.ltp).toBe(82);
    expect(next[0].call.ltp).toBe(100);
  });

  it("matches by contract_id even if the strike momentarily collides with another row", () => {
    const rows = [
      { strike: 24500, call: { contract_id: 1, ltp: 100 }, put: null },
      { strike: 24500, call: { contract_id: 99, ltp: 999 }, put: null }, // stale duplicate strike during a rollover
    ];
    const update = { contract_id: 1, underlying: "NIFTY", expiry: "2026-08-27", strike: 24500, option_type: "CE", ltp: 105 };

    const next = mergeOptionUpdateIntoRows(rows, update);

    expect(next[0].call.ltp).toBe(105); // the row whose CE leg actually has contract_id=1
    expect(next[1].call.ltp).toBe(999); // the unrelated row is untouched
  });

  it("falls back to strike+side matching when contract_id is absent", () => {
    const rows = [{ strike: 24500, call: { ltp: 100 }, put: null }];
    const update = { underlying: "NIFTY", expiry: "2026-08-27", strike: 24500, option_type: "CE", ltp: 110 };

    const next = mergeOptionUpdateIntoRows(rows, update);

    expect(next[0].call.ltp).toBe(110);
  });

  it("appends and re-sorts a brand-new strike not yet in the chain", () => {
    const rows = [{ strike: 24500, call: { ltp: 100 }, put: null }];
    const update = { underlying: "NIFTY", expiry: "2026-08-27", strike: 24400, option_type: "CE", ltp: 120 };

    const next = mergeOptionUpdateIntoRows(rows, update);

    expect(next.map((r) => r.strike)).toEqual([24400, 24500]);
  });

  it("never includes greeks/change_in_oi/iv keys the update did not carry, even for a new row", () => {
    const update = { underlying: "NIFTY", expiry: "2026-08-27", strike: 24600, option_type: "CE", ltp: 90 };
    const next = mergeOptionUpdateIntoRows([], update);

    expect("change_in_oi" in next[0].call).toBe(false);
    expect("iv" in next[0].call).toBe(false);
    expect("greeks" in next[0].call).toBe(false);
  });

  it("a full-snapshot update (with change_in_oi/iv/greeks present) does overwrite those fields", () => {
    const rows = [{ strike: 24500, call: { contract_id: 1, ltp: 100, change_in_oi: 200, iv: 14.0, greeks: { delta: 0.5 } }, put: null }];
    const update = {
      contract_id: 1, underlying: "NIFTY", expiry: "2026-08-27", strike: 24500, option_type: "CE",
      ltp: 103, change_in_oi: 250, iv: 14.5, greeks: { delta: 0.52 },
    };

    const next = mergeOptionUpdateIntoRows(rows, update);

    expect(next[0].call.change_in_oi).toBe(250);
    expect(next[0].call.iv).toBe(14.5);
    expect(next[0].call.greeks).toEqual({ delta: 0.52 });
  });
});
