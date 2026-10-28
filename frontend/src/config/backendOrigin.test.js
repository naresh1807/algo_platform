import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearBackendOrigin,
  getBackendOrigin,
  isCapacitorNative,
  isElectron,
  isWrappedApp,
  setBackendOrigin,
} from "./backendOrigin.js";

// vitest.config.js runs this suite under the "node" environment (no real
// DOM) -- window/localStorage don't exist here by default, so every test
// stubs exactly the shape config/backendOrigin.js actually reads from
// each (window.electronAPI.isElectron, window.Capacitor.isNativePlatform(),
// and the bare `localStorage` global), matching how Electron's preload.js
// and the Capacitor native runtime inject them for real.
function makeMemoryStorage() {
  const store = new Map();
  return {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  };
}

describe("config/backendOrigin", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", makeMemoryStorage());
    vi.stubGlobal("window", {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("plain browser build (no Electron/Capacitor globals) is always same-origin", () => {
    expect(isWrappedApp()).toBe(false);
    expect(getBackendOrigin()).toBe("");
  });

  it("Electron defaults to the local machine when nothing is configured yet", () => {
    vi.stubGlobal("window", { electronAPI: { isElectron: true } });
    expect(isElectron()).toBe(true);
    expect(getBackendOrigin()).toBe("http://127.0.0.1:8000");
  });

  it("a saved override always wins over the Electron default", () => {
    vi.stubGlobal("window", { electronAPI: { isElectron: true } });
    setBackendOrigin("http://192.168.1.50:8000");
    expect(getBackendOrigin()).toBe("http://192.168.1.50:8000");
  });

  it("Capacitor has no default -- Android can't guess where the backend is", () => {
    vi.stubGlobal("window", { Capacitor: { isNativePlatform: () => true } });
    expect(isCapacitorNative()).toBe(true);
    expect(isWrappedApp()).toBe(true);
    expect(getBackendOrigin()).toBe("");
  });

  it("setBackendOrigin trims whitespace and trailing slashes", () => {
    vi.stubGlobal("window", { electronAPI: { isElectron: true } });
    setBackendOrigin("  http://192.168.1.50:8000//  ");
    expect(getBackendOrigin()).toBe("http://192.168.1.50:8000");
  });

  it("clearBackendOrigin falls back to the wrapper's own default again", () => {
    vi.stubGlobal("window", { electronAPI: { isElectron: true } });
    setBackendOrigin("http://192.168.1.50:8000");
    clearBackendOrigin();
    expect(getBackendOrigin()).toBe("http://127.0.0.1:8000");
  });
});
