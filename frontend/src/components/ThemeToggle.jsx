import { Moon, Sun } from "lucide-react";

import { useThemeStore } from "../store/themeStore.js";

/**
 * Sun/Moon icon toggle -- themeStore.js already handles OS-detection,
 * localStorage persistence, and applying data-theme to <html>; this
 * component is purely the header control. Deliberately a separate
 * control from TradingModeSwitch (paper/live) -- the two must never be
 * visually or functionally conflated (spec requirement).
 */
export default function ThemeToggle() {
  const theme = useThemeStore((s) => s.theme);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);
  const isDark = theme === "dark";

  return (
    <button
      type="button"
      className="icon-btn"
      onClick={toggleTheme}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
    >
      {isDark ? <Moon size={16} /> : <Sun size={16} />}
    </button>
  );
}
