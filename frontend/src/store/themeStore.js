import { create } from "zustand";

const STORAGE_KEY = "theme";

/**
 * Dark/light theme choice, persisted to localStorage so a reload keeps
 * whatever the user last picked instead of always resetting to dark.
 * Applies the choice to <html data-theme="..."> immediately (both on
 * init and on every toggle) -- index.css keys its light-mode variable
 * overrides off that attribute, so this is the one place that has to
 * stay in sync with the CSS.
 */
function applyDomTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
}

function initialTheme() {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  // No stored preference yet -- default to the OS/browser's own
  // light/dark setting rather than always forcing dark.
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

const initial = initialTheme();
applyDomTheme(initial);

export const useThemeStore = create((set, get) => ({
  theme: initial,

  toggleTheme: () => {
    const next = get().theme === "dark" ? "light" : "dark";
    localStorage.setItem(STORAGE_KEY, next);
    applyDomTheme(next);
    set({ theme: next });
  },

  setTheme: (theme) => {
    localStorage.setItem(STORAGE_KEY, theme);
    applyDomTheme(theme);
    set({ theme });
  },
}));
