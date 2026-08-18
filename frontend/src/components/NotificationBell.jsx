import { Bell } from "lucide-react";

import { useJarvisStore } from "../store/jarvisStore.js";

/**
 * Reuses JARVIS's real proactive-announcement feed (apps.jarvis, pushed
 * over /ws/jarvis/live/, already wired in jarvisStore.js) as this
 * platform's notification system rather than inventing a second,
 * parallel one -- clicking it opens the same JARVIS panel that already
 * lists them (JarvisPanel.jsx handles its own open/close state; this
 * dispatches a DOM event it listens for, avoiding a new global store
 * field just for "is the panel open").
 */
export default function NotificationBell() {
  const unseenCount = useJarvisStore((s) => s.unseenCount);

  return (
    <button
      type="button"
      className="icon-btn"
      aria-label={unseenCount > 0 ? `${unseenCount} unread JARVIS notifications` : "Notifications"}
      title="JARVIS notifications"
      onClick={() => window.dispatchEvent(new CustomEvent("jarvis:open"))}
    >
      <Bell size={16} />
      {unseenCount > 0 && <span className="jarvis-badge" style={{ position: "absolute", top: -4, right: -4 }}>{unseenCount > 9 ? "9+" : unseenCount}</span>}
    </button>
  );
}
