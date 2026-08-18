/**
 * Generic loading placeholder -- `rows` for a list/table-shaped loader,
 * or `height` for a single block (e.g. a chart still fetching candles).
 * Deliberately shape-only (no shimmer text pretending to be real data).
 */
export default function LoadingSkeleton({ rows = 3, height }) {
  if (height) {
    return <div className="skeleton skeleton-block" style={{ height }} />;
  }
  return (
    <div>
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="skeleton skeleton-line"
          style={{ width: i === rows - 1 ? "60%" : "100%" }}
        />
      ))}
    </div>
  );
}
