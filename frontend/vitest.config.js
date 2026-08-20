import { defineConfig } from "vitest/config";

// Pure-logic unit tests only (no React rendering) -- see
// src/hooks/*.test.js for what's covered. "node" environment is enough
// since nothing here touches the DOM.
export default defineConfig({
  test: {
    environment: "node",
  },
});
