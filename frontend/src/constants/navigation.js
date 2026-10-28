import {
  Activity, ArrowLeftRight, BarChart3, Bot, ClipboardList, Cpu, GraduationCap,
  LayoutDashboard, LineChart, Newspaper, Settings as SettingsIcon,
  ShieldAlert, TrendingUp, Wallet,
} from "lucide-react";

export const NAV_SECTIONS = [
  { label: "Workspace", items: [
    { to: "/", label: "Home", icon: LayoutDashboard, end: true },
    { to: "/option-trading", label: "Trade options", icon: ArrowLeftRight },
    { to: "/delivery", label: "Stocks & holdings", icon: TrendingUp },
    { to: "/btst", label: "BTST signals", icon: TrendingUp },
    { to: "/orders", label: "Orders", icon: ClipboardList },
    { to: "/positions", label: "Portfolio", icon: Wallet },
  ] },
  { label: "Markets", items: [
    { to: "/jarvis-signals", label: "Trading workspace", icon: LineChart },
    { to: "/options", label: "Option chain & analytics", icon: BarChart3 },
    { to: "/investing", label: "Stock investing", icon: TrendingUp },
    { to: "/news", label: "Market news", icon: Newspaper },
  ] },
  { label: "Strategies & AI", items: [
    { to: "/paper-trading", label: "AI paper trading", icon: Cpu },
    { to: "/signals", label: "Trading signals", icon: Activity },
    { to: "/scalping", label: "Scalping strategies", icon: TrendingUp },
    { to: "/learning", label: "Learning & review", icon: GraduationCap },
  ] },
  { label: "Account insights", items: [
    { to: "/reports", label: "Performance reports", icon: BarChart3 },
    { to: "/risk", label: "Risk & activity", icon: ShieldAlert },
  ] },
];
export const MOBILE_TABS = [
  { to: "/", label: "Home", icon: LayoutDashboard, end: true },
  { to: "/option-trading", label: "Trade", icon: ArrowLeftRight },
  { to: "/orders", label: "Orders", icon: ClipboardList },
  { to: "/positions", label: "Portfolio", icon: Wallet },
];
export const SETTINGS_ITEM = { to: "/settings", label: "Settings", icon: SettingsIcon };
export const JARVIS_ICON = Bot;
export const PAGE_DETAILS = {
  "/btst": { section: "Strategies", title: "BTST signals", description: "Daily 2:30 PM market-day screening for next-session equity setups." },
  "/delivery": { section: "Trading", title: "Stocks & holdings", description: "Discover stocks, save your watchlist, and practice delivery buy/sell orders." },
  "/": { section: "Workspace", title: "Home", description: "Your markets, account, and trading activity at a glance." },
  "/option-trading": { section: "Trading", title: "Trade options", description: "Choose a call or put, review the risk, and manage your trade." },
  "/orders": { section: "Trading", title: "Orders", description: "Track the status and results of your manual option orders." },
  "/positions": { section: "Account", title: "Portfolio", description: "Open and closed trading positions, with net profit and loss." },
  "/jarvis-signals": { section: "Markets", title: "Trading workspace", description: "Charts, your watchlist, options, and AI analysis together." },
  "/options": { section: "Markets", title: "Option chain & analytics", description: "Explore strikes, open interest, volatility, and market positioning." },
  "/investing": { section: "Markets", title: "Stock investing", description: "Explore stocks, fundamentals, and market opportunities." },
  "/news": { section: "Markets", title: "Market news", description: "Follow the headlines and sentiment behind market moves." },
  "/paper-trading": { section: "Strategies & AI", title: "AI paper trading", description: "Follow simulated trades, decisions, and learning results." },
  "/signals": { section: "Strategies & AI", title: "Trading signals", description: "Understand each setup and why it was approved or rejected." },
  "/scalping": { section: "Strategies & AI", title: "Scalping strategies", description: "Review short-term setups and strategy performance." },
  "/learning": { section: "Strategies & AI", title: "Learning & review", description: "Review models, trade outcomes, and proposed strategy changes." },
  "/reports": { section: "Account insights", title: "Performance reports", description: "Review trading results, costs, and performance over time." },
  "/risk": { section: "Account insights", title: "Risk & activity", description: "Monitor account limits, risk events, and trading safeguards." },
  "/settings": { section: "Account", title: "Settings", description: "Manage your trading mode, preferences, and strategy settings." },
};
