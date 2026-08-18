import { useEffect, useRef, useState } from "react";
import { ShieldAlert, ShieldCheck } from "lucide-react";

import { useLiveStore } from "../store/liveStore.js";

/**
 * The kill switch is always visible per the spec -- but this platform's
 * backend deliberately has NO activate/deactivate endpoint (see
 * apps.risk.views.KillSwitchStatusView's own docstring and
 * apps.jarvis.engine.FORBIDDEN_ACTIONS, which permanently bars any
 * command from ever flipping it): re-arming is a manual, backend-only
 * management-command action by design, and activation only ever comes
 * from the risk engine itself on a real hard-limit breach. Building a
 * clickable "activate" control here would either silently no-op against
 * a real backend or require adding a new safety-critical endpoint this
 * redesign wasn't asked to add -- so this is a prominent, always-visible
 * STATUS indicator (reusing the same live killSwitchActive value
 * TopBar's banner already reads) with a popover explaining the real
 * safety lever a human actually has: the Paper/Live mode switch.
 */
export default function KillSwitchButton() {
  const killSwitchActive = useLiveStore((s) => s.killSwitchActive);
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onClickOutside = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open]);

  return (
    <div style={{ position: "relative" }} ref={ref}>
      <button
        type="button"
        className={`icon-btn${killSwitchActive ? " active" : ""}`}
        style={killSwitchActive ? { borderColor: "var(--red)", color: "var(--red)", background: "color-mix(in srgb, var(--red) 12%, transparent)" } : undefined}
        onClick={() => setOpen((v) => !v)}
        aria-label={killSwitchActive ? "Kill switch is ACTIVE -- entries halted" : "Kill switch inactive -- normal trading"}
        title={killSwitchActive ? "Kill switch ACTIVE" : "Kill switch inactive"}
      >
        {killSwitchActive ? <ShieldAlert size={16} /> : <ShieldCheck size={16} />}
      </button>
      {open && (
        <div className="kill-switch-popover" role="dialog">
          <strong>Kill switch: {killSwitchActive ? "ACTIVE" : "Inactive"}</strong>
          <p style={{ margin: "6px 0 0" }}>
            {killSwitchActive
              ? "All new entries are halted by the risk engine. Re-arming is a deliberate, backend-only action (never exposed in the UI, by design)."
              : "Trips automatically if a hard risk limit (drawdown, daily loss, etc.) is breached. There's no manual UI trigger for it, by design."}
          </p>
          <p style={{ margin: "6px 0 0" }}>
            To stop new real-money orders immediately yourself, switch to <strong>Paper</strong> mode in Settings.
          </p>
        </div>
      )}
    </div>
  );
}
