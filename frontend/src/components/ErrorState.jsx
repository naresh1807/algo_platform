import { AlertTriangle, RotateCw } from "lucide-react";

/**
 * A failed API/WebSocket-backed load. Always offers Retry when the
 * caller can actually retry (pass onRetry); never silently swaps in
 * fake data -- callers should keep showing this until a real refetch
 * succeeds.
 */
export default function ErrorState({ title = "Couldn't load this", detail, onRetry }) {
  return (
    <div className="state-block error">
      <AlertTriangle size={28} className="state-block-icon" />
      <div className="state-block-title">{title}</div>
      {detail && <div className="state-block-detail">{detail}</div>}
      {onRetry && (
        <button type="button" className="btn btn-sm" onClick={onRetry} style={{ marginTop: 4 }}>
          <RotateCw size={13} />
          Retry
        </button>
      )}
    </div>
  );
}
