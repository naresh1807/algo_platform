import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useLiveStore } from "../store/liveStore.js";

const AUTO_DISMISS_MS = 12_000; // longer than JarvisPanel's 8s toasts -- this is actionable (a real BUY signal), not routine chatter, worth a bit more time on screen.

/**
 * Floating popup for a live positive (BUY, approved) trading signal --
 * mounted once in App.jsx's AppShell, same pattern as JarvisPanel's own
 * toast stack, so it's visible from any page, not just the Dashboard.
 *
 * Fed by the SAME "signals_live" WebSocket the Dashboard's "Signal
 * Status" panel already reads (useLiveStore's `latestSignal`) -- no new
 * socket/subscription. apps.signals.engine only ever emits
 * signal_type="buy" with status="approved" (a genuine trade idea) or
 * signal_type="no_trade" with status="rejected" (see
 * backend/common/constants.py / apps/signals/engine.py) -- "sell" is
 * never produced today, so `buy && approved` is exactly and completely
 * what "the system found a positive signal" means in this codebase.
 */
export default function SignalAlertPopup() {
  const latestSignal = useLiveStore((s) => s.latestSignal);
  const [alerts, setAlerts] = useState([]);
  const lastSeenIdRef = useRef(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!latestSignal) return;
    if (latestSignal.signal_type !== "buy" || latestSignal.status !== "approved") return;
    if (latestSignal.id === lastSeenIdRef.current) return; // already shown this one
    lastSeenIdRef.current = latestSignal.id;

    const alert = { ...latestSignal, _uid: `${latestSignal.id}-${Date.now()}` };
    setAlerts((prev) => [...prev, alert].slice(-3));

    const timer = setTimeout(() => {
      setAlerts((prev) => prev.filter((a) => a._uid !== alert._uid));
    }, AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [latestSignal]);

  const dismiss = (uid) => setAlerts((prev) => prev.filter((a) => a._uid !== uid));

  if (alerts.length === 0) return null;

  return (
    <div className="signal-alert-stack">
      {alerts.map((a) => (
        <div
          key={a._uid}
          className="signal-alert-card"
          onClick={() => {
            dismiss(a._uid);
            navigate("/signals");
          }}
        >
          <div className="signal-alert-head">
            <span>🟢 BUY {a.symbol}</span>
            <button
              className="signal-alert-close"
              onClick={(e) => {
                e.stopPropagation();
                dismiss(a._uid);
              }}
            >
              ×
            </button>
          </div>
          <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 2 }}>
            score {a.total_score?.toFixed?.(2) ?? "—"} · {a.regime ?? "—"}
          </div>
          {(a.entry_price || a.stop_loss || a.target_1) && (
            <div className="signal-alert-levels">
              {a.entry_price && <span>Entry: {a.entry_price}</span>}
              {a.stop_loss && <span>SL: {a.stop_loss}</span>}
              {a.target_1 && <span>T1: {a.target_1}</span>}
              {a.target_2 && <span>T2: {a.target_2}</span>}
            </div>
          )}
          <div className="signal-alert-cta">View in Signals →</div>
        </div>
      ))}
    </div>
  );
}
