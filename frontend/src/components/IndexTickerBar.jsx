import { useEffect, useRef, useState } from "react";

import { endpoints } from "../services/api.js";
import { useLiveStore } from "../store/liveStore.js";

/**
 * apps.investing.Index / IndexConstituent -- "dashboard లో నిఫ్టీ బ్యాంక్
 * నిఫ్టీ సెన్సెక్స్ లాంటి అన్ని ఇండెక్స్‌లు పైన ప్రైస్ కనిపించాలి... వాటి
 * పైన క్లిక్ చేసినప్పుడు వాడి దగ్గర లోపల ఉన్న కంపెనీ స్టాక్ లిస్టు
 * కనిపించాలి" -- broker-app-style index bar with live price, click a
 * card to see that index's constituent stocks.
 *
 * Prices come from apps.investing.tasks.sync_index_constituents_and_prices
 * (every 3 min during market hours) -- real NSE/BSE data, not simulated
 * -- pushed over ws/investing/index-live/ (apps/investing/consumers.py)
 * the instant a new sync lands, on top of the initial REST fetch below.
 * See apps/investing/live_feed.py for why this is "pushed the moment
 * new data arrives" rather than genuine per-second ticks -- that needs
 * a real broker streaming connection this data source can't provide.
 * SENSEX (BSE) may show "not synced yet" until a working BSE session
 * populates it -- see apps.investing.bse_client's own honesty caveat.
 */
export default function IndexTickerBar() {
  const [indices, setIndices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [openIndex, setOpenIndex] = useState(null);
  const [constituents, setConstituents] = useState([]);
  const [constituentsLoading, setConstituentsLoading] = useState(false);

  const latestIndexUpdate = useLiveStore((s) => s.latestIndexUpdate);
  const connected = useLiveStore((s) => s.connected);

  useEffect(() => {
    endpoints.indices().then((res) => {
      setIndices(res.data.results ?? []);
      setLoading(false);
    });
  }, []);

  // Live merge -- Dashboard.jsx already calls useLiveStore.getState().connect()
  // on mount, so this component just subscribes to whatever arrives.
  useEffect(() => {
    if (!latestIndexUpdate) return;

    if (latestIndexUpdate.kind === "index_price") {
      setIndices((prev) =>
        prev.map((idx) =>
          idx.id === latestIndexUpdate.index_id
            ? {
                ...idx,
                latest_price: {
                  ltp: latestIndexUpdate.ltp,
                  change: latestIndexUpdate.change,
                  change_pct: latestIndexUpdate.change_pct,
                  timestamp: latestIndexUpdate.timestamp,
                },
              }
            : idx
        )
      );
    } else if (latestIndexUpdate.kind === "constituent_price" && openIndex?.id === latestIndexUpdate.index_id) {
      setConstituents((prev) =>
        prev.map((c) =>
          c.symbol === latestIndexUpdate.stock_symbol
            ? { ...c, last_price: latestIndexUpdate.last_price, change_pct: latestIndexUpdate.change_pct }
            : c
        )
      );
    }
  }, [latestIndexUpdate, openIndex]);

  // Guards against opening index A then quickly switching to index B
  // before A's constituents request resolves -- without this, A's
  // slower response could land AFTER B's and overwrite B's already-
  // displayed constituent list with A's stocks while the panel header
  // still reads "B".
  const constituentsRequestRef = useRef(0);

  const openCard = (index) => {
    if (openIndex?.id === index.id) {
      setOpenIndex(null);
      return;
    }
    const requestId = ++constituentsRequestRef.current;
    setOpenIndex(index);
    setConstituentsLoading(true);
    endpoints.indexConstituents(index.id).then((res) => {
      if (requestId !== constituentsRequestRef.current) return;
      setConstituents(res.data.results ?? []);
      setConstituentsLoading(false);
    });
  };

  if (loading) return null;
  if (indices.length === 0) {
    return (
      <div className="panel" style={{ marginBottom: 12 }}>
        <p style={{ color: "var(--muted)", fontSize: 13 }}>
          No indices seeded yet -- run <code>python manage.py seed_indices</code> on the backend.
        </p>
      </div>
    );
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
        <span className={`status-dot ${connected ? "ok" : "bad"}`} />
        <span style={{ fontSize: 11, color: "var(--muted)" }}>
          {connected ? "Live" : "Reconnecting…"}
        </span>
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        {indices.map((idx) => {
          const price = idx.latest_price;
          const up = price?.change_pct != null && price.change_pct >= 0;
          const active = openIndex?.id === idx.id;
          return (
            <button
              key={idx.id}
              onClick={() => openCard(idx)}
              style={{
                cursor: "pointer", textAlign: "left", minWidth: 150,
                background: "var(--panel)", border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`,
                borderRadius: 8, padding: "10px 14px",
              }}
            >
              <div style={{ fontSize: 12, color: "var(--muted)" }}>{idx.name}</div>
              {price ? (
                <>
                  <div style={{ fontSize: 18, fontWeight: 600, color: "var(--text)" }}>
                    {Number(price.ltp).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </div>
                  <div style={{ fontSize: 12, color: up ? "var(--green)" : "var(--red)" }}>
                    {price.change != null && `${up ? "+" : ""}${Number(price.change).toFixed(2)} `}
                    ({up ? "+" : ""}{price.change_pct?.toFixed(2)}%)
                  </div>
                </>
              ) : (
                <div style={{ fontSize: 12, color: "var(--muted)" }}>not synced yet</div>
              )}
              <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>
                {idx.constituent_count} stock(s)
              </div>
            </button>
          );
        })}
      </div>

      {openIndex && (
        <div className="panel" style={{ marginTop: 10 }}>
          <h3>{openIndex.name} — Constituents</h3>
          {constituentsLoading ? (
            <p style={{ color: "var(--muted)" }}>Loading…</p>
          ) : constituents.length === 0 ? (
            <p style={{ color: "var(--muted)", fontSize: 13 }}>
              No constituents synced yet for this index.
            </p>
          ) : (
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                  <th>Symbol</th><th>Company</th><th>Last Price</th><th>Change %</th><th>Weight</th>
                </tr>
              </thead>
              <tbody>
                {constituents
                  .sort((a, b) => (b.weight_pct ?? 0) - (a.weight_pct ?? 0))
                  .map((c) => (
                    <tr key={c.id} style={{ borderTop: "1px solid var(--border)" }}>
                      <td>{c.symbol}</td>
                      <td>{c.name}</td>
                      <td>{c.last_price ?? "—"}</td>
                      <td style={{ color: (c.change_pct ?? 0) >= 0 ? "var(--green)" : "var(--red)" }}>
                        {c.change_pct != null ? `${c.change_pct >= 0 ? "+" : ""}${c.change_pct.toFixed(2)}%` : "—"}
                      </td>
                      <td>{c.weight_pct != null ? `${c.weight_pct.toFixed(2)}%` : "—"}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
