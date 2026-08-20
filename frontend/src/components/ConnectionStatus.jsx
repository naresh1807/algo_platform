import { Radio, RadioTower, Wifi, WifiOff } from "lucide-react";

import { deriveFeedStatus, useSystemHealth } from "../hooks/useSystemHealth.js";
import { useLiveStore } from "../store/liveStore.js";
import StatusBadge from "./StatusBadge.jsx";

/**
 * Three independent real signals, shown side by side (never merged into
 * one ambiguous dot) -- this is deliberately NOT "Connected" based only
 * on the browser-to-Django WebSocket, per this platform's own
 * "distinguish the browser WebSocket from the actual Angel One feed"
 * requirement:
 *  - Browser WebSocket status: `connected` is true only once every one
 *    of liveStore's 5 sockets is open. liveStore auto-reconnects with
 *    backoff for the lifetime of the session, so "not connected" here
 *    always means "reconnecting", never a terminal "disconnected" state
 *    -- this says nothing about whether Angel One itself is streaming.
 *  - Broker feed health (REST-poll based): `feedHealthy` comes from
 *    apps.risk's feed_health WebSocket push, itself driven by
 *    apps.monitoring.FeedHealthCheck.
 *  - Live option feed detail: apps.monitoring.health.SystemHealthView
 *    (useSystemHealth/deriveFeedStatus) -- the actual Angel One
 *    WebSocket connection state, tick staleness, and priority-worker
 *    heartbeat, distinguishing "market closed" / "reconnecting" /
 *    "live feed stopped" / "priority worker missing" / "broker rate
 *    limited" / "option data stale" rather than one generic dot.
 */
export default function ConnectionStatus() {
  const connected = useLiveStore((s) => s.connected);
  const feedHealthy = useLiveStore((s) => s.feedHealthy);
  const { health, unreachable } = useSystemHealth();
  const feedStatus = deriveFeedStatus(health, unreachable);

  return (
    <div className="topbar-section" style={{ gap: 6 }}>
      <span title={connected ? "Browser WebSocket connected to the backend" : "Reconnecting the browser WebSocket to the backend…"}>
        <StatusBadge tone={connected ? "ok" : "warn"} icon={connected ? Wifi : WifiOff}>
          {connected ? "Live" : "Reconnecting"}
        </StatusBadge>
      </span>
      <span title={feedHealthy ? "Angel One broker feed healthy" : "Angel One broker feed degraded"}>
        <StatusBadge tone={feedHealthy ? "ok" : "bad"} icon={feedHealthy ? RadioTower : Radio}>
          Broker {feedHealthy ? "OK" : "Degraded"}
        </StatusBadge>
      </span>
      <span title={feedStatus.detail}>
        <StatusBadge tone={feedStatus.tone}>{feedStatus.label}</StatusBadge>
      </span>
    </div>
  );
}
