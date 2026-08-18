import { useEffect, useState } from "react";
import { Search } from "lucide-react";

import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";
import EmptyState from "./EmptyState.jsx";

/**
 * Compact, searchable, selectable watchlist -- same real data source
 * and live-merge pattern as IndexTickerBar.jsx (apps.investing.Index,
 * REST snapshot + /ws/investing/index-live/ push), just a narrow
 * vertical list instead of a horizontal card row, and restricted to
 * `symbols` (the underlyings this page actually cares about) rather
 * than every seeded index.
 */
export default function Watchlist({ symbols, selected, onSelect }) {
  const [indices, setIndices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const latestIndexUpdate = useLiveStore((s) => s.latestIndexUpdate);

  useEffect(() => {
    endpoints.indices().then((res) => {
      setIndices((res.data.results ?? []).filter((idx) => symbols.includes(idx.symbol)));
      setLoading(false);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbols.join(",")]);

  useEffect(() => {
    if (!latestIndexUpdate || latestIndexUpdate.kind !== "index_price") return;
    setIndices((prev) =>
      prev.map((idx) =>
        idx.id === latestIndexUpdate.index_id
          ? { ...idx, latest_price: { ltp: latestIndexUpdate.ltp, change: latestIndexUpdate.change, change_pct: latestIndexUpdate.change_pct } }
          : idx
      )
    );
  }, [latestIndexUpdate]);

  const filtered = indices.filter((idx) => idx.symbol.toLowerCase().includes(query.toLowerCase()));

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{ position: "relative", marginBottom: 8 }}>
        <Search size={14} style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)", color: "var(--muted)" }} />
        <input
          className="input"
          placeholder="Search watchlist…"
          aria-label="Search watchlist"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ width: "100%", paddingLeft: 28 }}
        />
      </div>

      {loading ? (
        <div className="skeleton skeleton-line" style={{ height: 40 }} />
      ) : filtered.length === 0 ? (
        <EmptyState title="No matching symbols" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, overflowY: "auto" }}>
          {filtered.map((idx) => {
            const price = idx.latest_price;
            const up = price?.change_pct != null && price.change_pct >= 0;
            const active = idx.symbol === selected;
            return (
              <button
                key={idx.id}
                type="button"
                onClick={() => onSelect(idx.symbol)}
                style={{
                  display: "flex", alignItems: "center", justifyContent: "space-between",
                  textAlign: "left", padding: "8px 10px", borderRadius: "var(--radius-sm)",
                  border: `1px solid ${active ? "var(--accent)" : "transparent"}`,
                  background: active ? "color-mix(in srgb, var(--accent) 10%, transparent)" : "transparent",
                  cursor: "pointer",
                }}
              >
                <span>
                  <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text)" }}>{idx.symbol}</div>
                  <div style={{ fontSize: 11, color: "var(--muted)" }}>{idx.name}</div>
                </span>
                {price ? (
                  <span style={{ textAlign: "right" }}>
                    <div className="num" style={{ fontSize: 13, color: "var(--text)" }}>
                      {Number(price.ltp).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                    </div>
                    <div style={{ fontSize: 11, color: up ? "var(--green)" : "var(--red)" }}>
                      {up ? "+" : ""}{price.change_pct?.toFixed(2)}%
                    </div>
                  </span>
                ) : (
                  <span style={{ fontSize: 11, color: "var(--muted)" }}>—</span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
