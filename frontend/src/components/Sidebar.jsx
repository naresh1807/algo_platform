import { NavLink, useNavigate } from "react-router-dom";

const LINKS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/signals", label: "Signals" },
  { to: "/news", label: "News" },
  { to: "/options", label: "Options Analytics" },
  { to: "/investing", label: "Stock Investing" },
  { to: "/positions", label: "Positions" },
  { to: "/scalping", label: "Scalping Strategies" },
  { to: "/risk", label: "Risk Log" },
  { to: "/learning", label: "Learning / Review" },
];

export default function Sidebar() {
  const navigate = useNavigate();

  const handleLogout = () => {
    localStorage.removeItem("authToken");
    navigate("/login");
  };

  return (
    <nav className="sidebar">
      {LINKS.map((link) => (
        <NavLink
          key={link.to}
          to={link.to}
          end={link.end}
          style={({ isActive }) => ({
            display: "block",
            padding: "8px 10px",
            borderRadius: 6,
            marginBottom: 4,
            color: isActive ? "#fff" : "var(--muted)",
            background: isActive ? "var(--accent)" : "transparent",
            textDecoration: "none",
            fontSize: 14,
          })}
        >
          {link.label}
        </NavLink>
      ))}
      <NavLink
        to="/settings"
        style={({ isActive }) => ({
          display: "block",
          marginTop: 16,
          padding: "8px 10px",
          borderRadius: 6,
          marginBottom: 4,
          color: isActive ? "#fff" : "var(--muted)",
          background: isActive ? "var(--accent)" : "transparent",
          textDecoration: "none",
          fontSize: 14,
        })}
      >
        Settings
      </NavLink>
      <button
        onClick={handleLogout}
        style={{
          background: "none", border: "none",
          color: "var(--red)", textAlign: "left", padding: "8px 10px",
          cursor: "pointer", fontSize: 14,
        }}
      >
        Log out
      </button>
    </nav>
  );
}
