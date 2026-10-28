import { afterEach, describe, expect, it, vi } from "vitest";
import { newOrderRequestId, readPendingOrder, rememberPendingOrder } from "./orderRequest.js";

afterEach(() => vi.unstubAllGlobals());

describe("option order request recovery", () => {
  it("creates valid distinct UUIDs when randomUUID is unavailable on HTTP", () => {
    let next = 0;
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes) => { bytes.fill(++next); return bytes; },
    });
    const first = newOrderRequestId();
    expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(newOrderRequestId()).not.toBe(first);
  });
  it("restores the identical request payload after reload and clears completed requests", () => {
    const memory = new Map();
    vi.stubGlobal("sessionStorage", {
      getItem: (key) => memory.get(key),
      setItem: (key, value) => memory.set(key, value),
      removeItem: (key) => memory.delete(key),
    });
    const order = { request_id: "same-order-id", action: "buy", lots: 1, contract_id: 23 };
    rememberPendingOrder(order);
    expect(readPendingOrder()).toEqual(order);
    rememberPendingOrder(null);
    expect(readPendingOrder()).toBeNull();
  });
  it("tolerates unavailable storage", () => {
    vi.stubGlobal("sessionStorage", {
      getItem: () => { throw new Error("storage blocked"); },
      setItem: () => { throw new Error("storage blocked"); },
    });
    expect(readPendingOrder()).toBeNull();
    expect(() => rememberPendingOrder({ request_id: "id" })).not.toThrow();
  });
});

