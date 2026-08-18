import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown, Search } from "lucide-react";

import EmptyState from "./EmptyState.jsx";
import ErrorState from "./ErrorState.jsx";
import LoadingSkeleton from "./LoadingSkeleton.jsx";

/**
 * One generic, sortable/searchable table -- this is what the spec's
 * "PositionsTable"/"OrdersTable"/etc. collapse into: every trading
 * table in this app (Positions, Signals, Reports' breakdown tables)
 * shares the exact same shape (columns + rows + sort + search), so a
 * single configurable component avoids maintaining several near-
 * identical copies of the same sort/search logic.
 *
 * `columns`: [{ key, label, render?(row), sortValue?(row), align?,
 *   sortable? }] -- render() controls display, sortValue() (falling
 * back to row[key]) controls ordering, so a column can display a
 * formatted string while still sorting numerically.
 * `searchKeys`: which column keys free-text search filters against
 * (uses row[key] as a string; omit to disable the search box).
 */
export default function DataTable({
  columns, rows, getRowKey, searchKeys, searchPlaceholder = "Search…",
  loading = false, error = null, onRetry, onRowClick,
  emptyTitle = "Nothing here yet", emptyDetail,
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState({ key: null, dir: "asc" });

  const filtered = useMemo(() => {
    if (!searchKeys || !query.trim()) return rows;
    const q = query.trim().toLowerCase();
    return rows.filter((row) => searchKeys.some((key) => String(row[key] ?? "").toLowerCase().includes(q)));
  }, [rows, query, searchKeys]);

  const sorted = useMemo(() => {
    if (!sort.key) return filtered;
    const col = columns.find((c) => c.key === sort.key);
    const getValue = col?.sortValue ?? ((row) => row[sort.key]);
    const copy = [...filtered];
    copy.sort((a, b) => {
      const av = getValue(a);
      const bv = getValue(b);
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (av < bv) return sort.dir === "asc" ? -1 : 1;
      if (av > bv) return sort.dir === "asc" ? 1 : -1;
      return 0;
    });
    return copy;
  }, [filtered, sort, columns]);

  const toggleSort = (key) => {
    setSort((prev) => (prev.key === key ? { key, dir: prev.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  };

  if (loading) return <LoadingSkeleton rows={4} />;
  if (error) return <ErrorState detail={error} onRetry={onRetry} />;

  return (
    <div>
      {searchKeys && (
        <div style={{ position: "relative", marginBottom: 10, maxWidth: 280 }}>
          <Search size={14} style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)", color: "var(--muted)" }} />
          <input
            className="input"
            placeholder={searchPlaceholder}
            aria-label={searchPlaceholder}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ width: "100%", paddingLeft: 28 }}
          />
        </div>
      )}

      {sorted.length === 0 ? (
        <EmptyState title={emptyTitle} detail={emptyDetail} />
      ) : (
        <div className="data-table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                {columns.map((col) => (
                  <th
                    key={col.key}
                    className={col.sortable !== false ? "sortable" : undefined}
                    onClick={col.sortable !== false ? () => toggleSort(col.key) : undefined}
                    style={col.align ? { textAlign: col.align } : undefined}
                  >
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      {col.label}
                      {col.sortable !== false && (
                        sort.key === col.key
                          ? (sort.dir === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />)
                          : <ArrowUpDown size={11} style={{ opacity: 0.4 }} />
                      )}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((row) => (
                <tr key={getRowKey(row)} onClick={onRowClick ? () => onRowClick(row) : undefined} style={onRowClick ? { cursor: "pointer" } : undefined}>
                  {columns.map((col) => (
                    <td key={col.key} style={col.align ? { textAlign: col.align } : undefined}>
                      {col.render ? col.render(row) : row[col.key]}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
