import {
  Activity, BarChart3, Bot, GraduationCap, LayoutDashboard, LineChart,
  Newspaper, Settings as SettingsIcon, ShieldAlert, TrendingUp, Wallet,
} from "lucide-react";

/**
 * Sidebar structure, grouped into sections. Adapted from the spec's
 * recommended IA (Dashboard/Markets/Trading/Portfolio/Strategies/AI/
 * Risk/Reports/Settings) onto this platform's REAL routes -- there is
 * no separate "Order Entry" or "Live Trading" page (trading is fully
 * automated; Paper/Live is a mode toggle inside Settings, not a page),
 * so those spec labels are folded into the pages that actually exist
 * rather than linking to routes that would 404.
 */
export const NAV_SECTIONS = [
  {
    label: "Overview",
    items: [{ to: "/", label: "Dashboard", icon: LayoutDashboard, end: true }],
  },
  {
    label: "Trading",
    items: [
      { to: "/jarvis-signals", label: "Trading Workspace", icon: LineChart },
      { to: "/signals", label: "Signals", icon: Activity },
      { to: "/options", label: "Options Analytics", icon: BarChart3 },
      { to: "/scalping", label: "Scalping Strategies", icon: TrendingUp },
    ],
  },
  {
    label: "Portfolio",
    items: [
      { to: "/positions", label: "Positions", icon: Wallet },
      { to: "/reports", label: "Performance", icon: BarChart3 },
    ],
  },
  {
    label: "Markets",
    items: [
      { to: "/investing", label: "Stock Investing", icon: TrendingUp },
      { to: "/news", label: "News", icon: Newspaper },
    ],
  },
  {
    label: "Intelligence",
    items: [
      { to: "/learning", label: "Learning / Review", icon: GraduationCap },
    ],
  },
  {
    label: "Risk",
    items: [{ to: "/risk", label: "Risk Log", icon: ShieldAlert }],
  },
];

export const SETTINGS_ITEM = { to: "/settings", label: "Settings", icon: SettingsIcon };

// Used by JarvisPanel's FAB / a future badge -- kept here since it's
// identity, not layout, config.
export const JARVIS_ICON = Bot;
