import { Inbox } from "lucide-react";

/**
 * "There is genuinely no data yet" -- distinct from ErrorState (a call
 * failed) and LoadingSkeleton (a call is in flight). Used for empty
 * lists/tables so a blank panel never looks broken or unfinished.
 */
export default function EmptyState({ icon: Icon = Inbox, title = "Nothing here yet", detail, action }) {
  return (
    <div className="state-block">
      <Icon size={28} className="state-block-icon" />
      <div className="state-block-title">{title}</div>
      {detail && <div className="state-block-detail">{detail}</div>}
      {action}
    </div>
  );
}
