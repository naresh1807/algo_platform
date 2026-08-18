import { create } from "zustand";

const STORAGE_KEY = "sidebarCollapsed";

/**
 * Pure layout/chrome state (sidebar collapse today) -- deliberately its
 * own store, separate from themeStore.js (light/dark) and liveStore.js
 * (WebSocket data), since it's neither a persisted user preference tied
 * to color scheme nor server-pushed data. Persisted to localStorage the
 * same way themeStore already does, so a collapsed sidebar stays
 * collapsed across a refresh.
 */
function initialCollapsed() {
  return localStorage.getItem(STORAGE_KEY) === "true";
}

export const useUiStore = create((set, get) => ({
  sidebarCollapsed: initialCollapsed(),

  toggleSidebar: () => {
    const next = !get().sidebarCollapsed;
    localStorage.setItem(STORAGE_KEY, String(next));
    set({ sidebarCollapsed: next });
  },
}));
