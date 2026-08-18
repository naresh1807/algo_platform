const TONE_CLASS = {
  ok: "badge-green",
  warn: "badge-amber",
  bad: "badge-red",
  info: "badge-accent",
  muted: "badge-muted",
  jarvis: "badge-jarvis",
};

/**
 * Small pill used everywhere a status needs a color + a label together
 * (never color alone, per the accessibility requirement) -- signal
 * status, risk severity, connection state, order/position state, etc.
 * `tone` picks the semantic color; `icon` is optional (an already-
 * imported lucide-react component).
 */
export default function StatusBadge({ tone = "muted", icon: Icon, children }) {
  return (
    <span className={`badge ${TONE_CLASS[tone] ?? TONE_CLASS.muted}`}>
      {Icon && <Icon size={11} />}
      {children}
    </span>
  );
}
