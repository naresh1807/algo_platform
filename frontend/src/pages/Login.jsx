import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../services/api.js";

/**
 * This page didn't exist until now -- apps.auth_app.urls already
 * exposed /api/auth/token/ (DRF's built-in obtain_auth_token) since
 * Phase 1, but nothing in the frontend ever called it. Every REST
 * request went out with no Authorization header, and
 * settings.REST_FRAMEWORK's DEFAULT_PERMISSION_CLASSES=[IsAuthenticated]
 * rejected all of them with 403 -- which is exactly the error the
 * screenshots showed. This page is the missing other half of that.
 *
 * Use the superuser you created with `python manage.py createsuperuser`.
 */
export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api.post("/auth/token/", { username, password });
      localStorage.setItem("authToken", res.data.token);
      navigate("/");
    } catch (err) {
      setError(
        err.response?.status === 400
          ? "Invalid username or password."
          : "Could not reach the backend -- is Django running on 127.0.0.1:8000?"
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        height: "100vh", background: "var(--bg)",
      }}
    >
      <form
        onSubmit={handleSubmit}
        className="panel"
        style={{ width: 320, display: "flex", flexDirection: "column", gap: 12 }}
      >
        <h3 style={{ marginBottom: 4 }}>Algo Trading Platform</h3>
        <input
          type="text"
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
          style={{
            padding: 8, borderRadius: 6, border: "1px solid var(--border)",
            background: "var(--bg)", color: "var(--text)",
          }}
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          style={{
            padding: 8, borderRadius: 6, border: "1px solid var(--border)",
            background: "var(--bg)", color: "var(--text)",
          }}
        />
        {error && <p style={{ color: "var(--red)", fontSize: 13, margin: 0 }}>{error}</p>}
        <button
          type="submit"
          disabled={loading}
          style={{
            padding: 10, borderRadius: 6, border: "none",
            background: "var(--accent)", color: "#fff", cursor: "pointer",
            fontWeight: 600,
          }}
        >
          {loading ? "Signing in…" : "Sign in"}
        </button>
        <p style={{ fontSize: 12, color: "var(--muted)", margin: 0 }}>
          Use the superuser account from `python manage.py createsuperuser`.
        </p>
      </form>
    </div>
  );
}
