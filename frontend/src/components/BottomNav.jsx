import { useEffect, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { Menu, X, LogOut, ArrowUpRight } from "lucide-react";
import { MOBILE_TABS, NAV_SECTIONS, SETTINGS_ITEM } from "../constants/navigation.js";
import { logoutSession } from "../services/api.js";
import AppDialog from "./AppDialog.jsx";

const PRIMARY_PATHS = new Set(MOBILE_TABS.map((tab) => tab.to));
export default function BottomNav() {
  const navigate = useNavigate();
  const location = useLocation();
  const [moreOpen, setMoreOpen] = useState(false);
  useEffect(() => setMoreOpen(false), [location.pathname]);
  const handleLogout = async () => {
    setMoreOpen(false);
    await logoutSession();
    navigate("/login", { replace: true });
  };
  const tabClass = ({ isActive }) => `bottom-nav-tab${isActive ? " active" : ""}`;
  return <>
    <AppDialog open={moreOpen} onClose={() => setMoreOpen(false)} className="mobile-navigation-dialog" label="More navigation">
      <div className="mobile-menu-handle" />
      <div className="mobile-menu-heading"><div><span className="page-eyebrow">Your workspace</span><h2>Explore more</h2></div>
        <button className="icon-btn" onClick={() => setMoreOpen(false)} aria-label="Close navigation"><X size={20} /></button>
      </div>
      <div className="mobile-menu-body">
        {NAV_SECTIONS.map((section) => {
          const items = section.items.filter((item) => !PRIMARY_PATHS.has(item.to));
          if (!items.length) return null;
          return <section key={section.label}><h3>{section.label}</h3><div className="mobile-menu-grid">
            {items.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} className="mobile-menu-link" onClick={() => setMoreOpen(false)}>
              <Icon size={21} /><span>{label}</span><ArrowUpRight size={14} />
            </NavLink>)}
          </div></section>;
        })}
        <div className="mobile-account-actions">
          <NavLink to={SETTINGS_ITEM.to} onClick={() => setMoreOpen(false)}><SETTINGS_ITEM.icon size={18} />Settings</NavLink>
          <button onClick={handleLogout}><LogOut size={18} />Log out</button>
        </div>
      </div>
    </AppDialog>
    <nav className="bottom-nav" aria-label="Mobile navigation">
      {MOBILE_TABS.map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} className={tabClass}>
        <span className="bottom-nav-icon"><Icon size={21} /></span><span>{label}</span>
      </NavLink>)}
      <button type="button" className={`bottom-nav-tab${moreOpen ? " active" : ""}`} onClick={() => setMoreOpen(true)} aria-haspopup="dialog" aria-expanded={moreOpen}>
        <span className="bottom-nav-icon"><Menu size={21} /></span><span>More</span>
      </button>
    </nav>
  </>;
}
