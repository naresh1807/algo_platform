import { Radio, RadioTower, Wifi, WifiOff } from "lucide-react";

import { useLiveStore } from "../store/liveStore.js";
import StatusBadge from "./StatusBadge.jsx";

/**
 * Two independent real signals from liveStore.js, shown side by side
 * (never merged into one ambiguous dot):
 *  - WebSocket status: `connected` is true only once every one of the
 *    5 sockets is open. liveStore auto-reconnects with backoff for the
 *    lifetime of the session (see that module's docstring) and never
 *    gives up, so "not connected" here always means "reconnecting",
 *    never a terminal "disconnected" state.
 *  - Broker/feed health: `feedHealthy` comes from apps.risk's
 *    feed_health WebSocket push, itself driven by
 *    apps.monitoring.FeedHealthCheck -- this IS the real Angel One
 *    broker-feed health signal, not a separate invented concept.
 */
export default function ConnectionStatus() {
  const connected = useLiveStore((s) => s.connected);
  const feedHealthy = useLiveStore((s) => s.feedHealthy);

  return (
    <div className="topbar-section" style={{ gap: 6 }}>
      <span title={connected ? "Live WebSocket feed connected" : "Reconnecting to live feed…"}>
        <StatusBadge tone={connected ? "ok" : "warn"} icon={connected ? Wifi : WifiOff}>
          {connected ? "Live" : "Reconnecting"}
        </StatusBadge>
      </span>
      <span title={feedHealthy ? "Angel One broker feed healthy" : "Angel One broker feed degraded"}>
        <StatusBadge tone={feedHealthy ? "ok" : "bad"} icon={feedHealthy ? RadioTower : Radio}>
          Broker {feedHealthy ? "OK" : "Degraded"}
        </StatusBadge>
      </span>
    </div>
  );
}
