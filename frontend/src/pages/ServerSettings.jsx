import { useState } from "react";
import axios from "axios";
import { Link, useNavigate } from "react-router-dom";

import { getBackendOrigin, isElectron, setBackendOrigin } from "../config/backendOrigin.js";

/**
 * Only reachable inside the Electron/Capacitor wrapper shells (see
 * App.jsx's RequireServerConfigured) -- the plain browser build always
 * has a same-origin backend and never needs this page.
 *
 * Desktop (Electron) already knows where its backend is (electron/main.js
 * launches it on the same machine and defaults getBackendOrigin() to
 * http://127.0.0.1:8000, see config/backendOrigin.js), so a desktop user
 * only lands here if they explicitly want to point at a different
 * backend (e.g. the cloud-hosted one instead of their own machine).
 * Android has no such default -- this is the mandatory first-run screen
 * there, same chicken-and-egg problem Login.jsx would otherwise hit
 * (it can't call /api/auth/token/ against an address nobody has typed
 * in yet).
 */
export default function ServerSettings() {
  const [url, setUrl] = useState(getBackendOrigin() || (isElectron() ? "http://127.0.0.1:8000" : ""));
  const [status, setStatus] = useState(null); // null | "testing" | "ok" | "error"
  const [error, setError] = useState(null);
  const navigate = useNavigate();

  const normalized = () => url.trim().replace(/\/+$/, "");

  const handleTest = async () => {
    setStatus("testing");
    setError(null);
    try {
      // A one-off request, deliberately not going through services/api.js's
      // shared `api` instance -- that instance's baseURL is fixed at
      // module load to whatever was already saved, which is exactly the
      // value this button exists to check *before* saving.
      await axios.get(`${normalized()}/api/monitoring/health/`, { timeout: 5000 });
      setStatus("ok");
    } catch (err) {
      // Real bug, found live: a 401 here is the NORMAL, correct response
      // (this runs before login, so there's never a token yet) -- it was
      // being shown as an alarming "check the address" error even though
      // a real HTTP response arriving at all is exactly what proves the
      // address is right and a real instance of this backend answered.
      // Only a true network failure (no response at all) means the
      // address itself is wrong.
      if (err.response) {
        setStatus("ok");
      } else {
        setStatus("error");
        setError(
          "Could not reach that address. Check the URL, that the backend is running, and that this device is on the same network."
        );
      }
    }
  };

  const handleSave = (e) => {
    e.preventDefault();
    setBackendOrigin(normalized());
    // Full navigation (not client-side routing) is deliberate: api.js and
    // liveStore.js read getBackendOrigin() once at module load, so a
    // real reload is what makes the new address take effect.
    window.location.href = "/login";
  };

  return (
    <div
      style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        height: "100vh", background: "var(--bg)",
      }}
    >
      <form
        onSubmit={handleSave}
        className="panel"
        style={{ width: 360, display: "flex", flexDirection: "column", gap: 12 }}
      >
        <h3 style={{ marginBottom: 4 }}>Connect to your backend</h3>
        <p style={{ fontSize: 13, color: "var(--muted)", margin: 0 }}>
          Enter the address of the Django backend this app should talk to -- e.g. your
          PC's address on the same Wi-Fi (<code>http://192.168.1.23:8000</code>), or a
          cloud-hosted URL if you've set one up.
        </p>
        <input
          type="text"
          placeholder="http://192.168.1.23:8000"
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setStatus(null);
          }}
          autoFocus
          style={{
            padding: 8, borderRadius: 6, border: "1px solid var(--border)",
            background: "var(--bg)", color: "var(--text)",
          }}
        />
        <button
          type="button"
          onClick={handleTest}
          disabled={!url.trim() || status === "testing"}
          style={{
            padding: 8, borderRadius: 6, border: "1px solid var(--border)",
            background: "transparent", color: "var(--text)", cursor: "pointer",
          }}
        >
          {status === "testing" ? "Testing…" : "Test connection"}
        </button>
        {status === "ok" && (
          <p style={{ color: "var(--green)", fontSize: 13, margin: 0 }}>Reachable.</p>
        )}
        {status === "error" && (
          <p style={{ color: "var(--red)", fontSize: 13, margin: 0 }}>{error}</p>
        )}
        <button
          type="submit"
          disabled={!url.trim()}
          style={{
            padding: 10, borderRadius: 6, border: "none",
            background: "var(--accent)", color: "#fff", cursor: "pointer",
            fontWeight: 600,
          }}
        >
          Save and continue
        </button>
        {getBackendOrigin() && (
          <button
            type="button"
            onClick={() => navigate("/login")}
            style={{
              padding: 8, borderRadius: 6, border: "none",
              background: "transparent", color: "var(--muted)", cursor: "pointer",
              fontSize: 13,
            }}
          >
            Cancel
          </button>
        )}
      </form>
    </div>
  );
}

export function ChangeServerLink() {
  return (
    <Link to="/server-settings" style={{ fontSize: 12, color: "var(--muted)" }}>
      Change server address
    </Link>
  );
}
