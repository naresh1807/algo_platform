import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";

// Cheap (no broker calls -- see backend apps.monitoring.health's own
// docstring) and only needs to be "close enough" for a status badge, so
// a modest poll interval keeps this from being a meaningful source of
// backend load on its own.
const POLL_INTERVAL_MS = 15000;

/**
 * Polls apps.monitoring.health.SystemHealthView -- the ONE place this
 * frontend learns whether the actual Angel One live feed (not just the
 * browser-to-Django WebSocket) is healthy: connection state, tick
 * staleness, subscribed option-token count, priority Celery worker
 * heartbeat, and the latest detected error category. See
 * deriveFeedStatus below for how this is turned into a single
 * human status a badge can show.
 */
export function useSystemHealth() {
  const [health, setHealth] = useState(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      endpoints
        .systemHealth()
        .then((res) => {
          if (cancelled) return;
          setHealth(res.data);
          setUnreachable(false);
        })
        .catch(() => {
          if (!cancelled) setUnreachable(true);
        });
    };
    load();
    const id = setInterval(load, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return { health, unreachable };
}

const RATE_LIMIT_ERROR_HINTS = ["rate limit", "ab1021", "too many requests", "access denied"];

/**
 * Reduces the full health payload into ONE {tone, label, detail} the
 * badge renders -- ordered so the most actionable/severe condition wins
 * when several are true at once (e.g. market closed AND the feed
 * happens to be reconnecting -- "Market Closed" is the one that
 * actually explains what the trader is seeing).
 */
export function deriveFeedStatus(health, unreachable) {
  if (unreachable || !health) {
    return { tone: "bad", label: "Health Unknown", detail: "Could not reach the backend health endpoint." };
  }
  if (!health.market_open) {
    return { tone: "muted", label: "Market Closed", detail: health.market_status_detail || "Outside NSE trading hours." };
  }

  const lastErrorCategory = (health.live_feed?.last_error?.category || "").toLowerCase();
  if (RATE_LIMIT_ERROR_HINTS.some((hint) => lastErrorCategory.includes(hint))) {
    return { tone: "bad", label: "Broker Rate Limited", detail: health.live_feed.last_error.detail || "Angel One rate limit recently hit." };
  }

  const state = health.live_feed?.connection_state;
  if (!state || state === "stopped" || health.live_feed?.process_heartbeat?.stale) {
    return { tone: "bad", label: "Live Feed Stopped", detail: "python manage.py run_live_feed does not appear to be running." };
  }
  if (state === "connecting" || state === "reconnecting") {
    return { tone: "warn", label: "Feed Reconnecting", detail: "The Angel One WebSocket connection is not currently open." };
  }
  if (health.celery_priority_worker?.stale) {
    return { tone: "warn", label: "Priority Worker Missing", detail: "The priority Celery worker (-Q priority) has no recent heartbeat." };
  }
  if (health.live_feed?.last_option_tick?.stale) {
    return { tone: "warn", label: "Option Data Stale", detail: "No option tick received recently despite the feed being connected." };
  }
  return { tone: "ok", label: "Feed OK", detail: `${health.live_feed?.subscribed_option_token_count ?? 0} option tokens subscribed.` };
}
