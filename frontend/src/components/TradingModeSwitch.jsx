import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { FlaskConical, Zap } from "lucide-react";

import { endpoints } from "../services/api.js";

/**
 * Header read-out of apps.execution.ExecutionModeSetting (paper/live) --
 * the actual mode-SWITCHING control (with its typed-confirmation guard
 * for going live) already lives on the Settings page
 * (apps.execution.views.ExecutionModeView via Settings.jsx) and stays
 * there rather than being duplicated in the header, since flipping to
 * LIVE is a deliberately-frictionful action the manual keeps behind a
 * confirm phrase -- a one-click header toggle would undermine that.
 * This is a live, honest STATUS pill that link-throughs to Settings,
 * not a second control surface for the same state.
 */
export default function TradingModeSwitch() {
  const [mode, setMode] = useState(null);
  const navigate = useNavigate();

  useEffect(() => {
    endpoints.executionMode().then((res) => setMode(res.data.mode)).catch(() => {});
  }, []);

  if (!mode) return null;
  const isLive = mode === "live";

  return (
    <button
      type="button"
      onClick={() => navigate("/settings")}
      title={isLive ? "LIVE trading is active -- real orders place with real money. Click to manage." : "Paper trading -- simulated fills only. Click to manage."}
      className="btn btn-sm"
      style={{
        borderColor: isLive ? "var(--red)" : "var(--border)",
        color: isLive ? "var(--red)" : "var(--muted)",
        background: isLive ? "color-mix(in srgb, var(--red) 10%, transparent)" : "transparent",
        fontWeight: 700,
      }}
    >
      {isLive ? <Zap size={13} /> : <FlaskConical size={13} />}
      {isLive ? "LIVE" : "PAPER"}
    </button>
  );
}
