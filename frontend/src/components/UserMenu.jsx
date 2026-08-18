import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { LogOut, Settings as SettingsIcon, User } from "lucide-react";

/**
 * Minimal profile menu. There is no /auth/me or user-profile endpoint
 * in this backend (DRF token auth is opaque -- the token carries no
 * decodable username), so this deliberately does NOT fabricate a
 * display name or avatar; it offers only the two real, working actions
 * available: go to Settings, and log out (same logic Sidebar.jsx used
 * to own, moved here since it's a profile-menu action, not navigation).
 */
export default function UserMenu() {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!open) return undefined;
    const onClickOutside = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open]);

  const handleLogout = () => {
    localStorage.removeItem("authToken");
    navigate("/login");
  };

  return (
    <div style={{ position: "relative" }} ref={ref}>
      <button
        type="button"
        className="icon-btn"
        aria-label="User menu"
        title="Account"
        onClick={() => setOpen((v) => !v)}
      >
        <User size={16} />
      </button>
      {open && (
        <div className="kill-switch-popover" role="menu" style={{ width: 180, padding: "var(--space-2)" }}>
          <button
            type="button"
            className="btn btn-ghost"
            style={{ width: "100%", justifyContent: "flex-start" }}
            onClick={() => {
              setOpen(false);
              navigate("/settings");
            }}
          >
            <SettingsIcon size={14} /> Settings
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            style={{ width: "100%", justifyContent: "flex-start", color: "var(--red)" }}
            onClick={handleLogout}
          >
            <LogOut size={14} /> Log out
          </button>
        </div>
      )}
    </div>
  );
}
