import { Link, NavLink, useNavigate } from "react-router-dom";
import { ArrowUpRight, LogOut } from "lucide-react";

import { NAV_SECTIONS, SETTINGS_ITEM } from "../constants/navigation.js";
import { logoutSession } from "../services/api.js";
import { useUiStore } from "../store/uiStore.js";

export default function Sidebar() {
  const navigate = useNavigate();
  const sidebarCollapsed = useUiStore((s) => s.sidebarCollapsed);

  const handleLogout = async () => {
    await logoutSession();
    navigate("/login", { replace: true });
  };

  const linkClass = ({ isActive }) => `sidebar-link${isActive ? " active" : ""}`;

  return (
    <nav className="sidebar" aria-label="Main navigation">
      <Link className="sidebar-trade-cta" to="/option-trading" aria-label="Place an option order"><span>Place an option order</span><ArrowUpRight size={18} /></Link>
      {NAV_SECTIONS.map((section) => (
        <div className="sidebar-section" key={section.label}>
          {!sidebarCollapsed && <div className="sidebar-section-label">{section.label}</div>}
          {section.items.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={linkClass} aria-label={label} data-tooltip={label}>
              <Icon size={17} />
              <span className="sidebar-link-label">{label}</span>
            </NavLink>
          ))}
        </div>
      ))}

      <div className="sidebar-footer">
        <NavLink to={SETTINGS_ITEM.to} className={linkClass} aria-label={SETTINGS_ITEM.label} data-tooltip={SETTINGS_ITEM.label}>
          <SETTINGS_ITEM.icon size={17} />
          <span className="sidebar-link-label">{SETTINGS_ITEM.label}</span>
        </NavLink>
        <button type="button" className="logout-link" onClick={handleLogout} aria-label="Log out" data-tooltip="Log out">
          <LogOut size={17} />
          <span className="sidebar-link-label">Log out</span>
        </button>
      </div>
    </nav>
  );
}
