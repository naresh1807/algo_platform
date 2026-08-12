import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";

const VERDICT_COLOR = {
  strong_hold: "var(--green)",
  moderate_hold: "var(--accent)",
  watch: "var(--muted)",
  avoid: "var(--red)",
};

const VALUATION_COLOR = {
  Low: "var(--green)",
  Medium: "var(--muted)",
  High: "var(--red)",
};

const TREND_COLOR = {
  "Strong Uptrend": "var(--green)",
  "Uptrend": "var(--green)",
  "Sideways / Mixed": "var(--muted)",
  "Downtrend": "var(--red)",
  "Strong Downtrend": "var(--red)",
};

const VERDICT_LABEL = {
  strong_hold: "Strong Hold",
  moderate_hold: "Moderate Hold",
  watch: "Watch",
  avoid: "Avoid",
};

/**
 * apps.investing -- long-term equity investing. Not part of the
 * manual (no chapter covers this at all -- every other page in this
 * app is intraday index trading); built because the trader asked for
 * automatic stock suggestions with a suggested holding period, backed
 * by real fundamentals, plus a watchlist and IPO suggestions.
 *
 * Every score/verdict/hold-period shown here comes straight from
 * apps.investing.fundamental_score's stored StockRecommendation rows
 * -- nothing is computed client-side, and nothing is fabricated when
 * data isn't available yet (see the empty-state messaging below).
 */
export default function StockInvesting() {
  const [watchlist, setWatchlist] = useState([]);
  const [recommendations, setRecommendations] = useState([]);
  const [ipos, setIpos] = useState([]);
  const [sectors, setSectors] = useState([]);
  const [loading, setLoading] = useState(true);
  const [newSymbol, setNewSymbol] = useState("");
  const [error, setError] = useState(null);

  const loadAll = () => {
    Promise.allSettled([
      endpoints.stockWatchlist(),
      endpoints.stockRecommendations(),
      endpoints.ipoListings(),
      endpoints.sectorBreakdown(),
    ]).then(([w, r, i, s]) => {
      if (w.status === "fulfilled") setWatchlist(w.value.data.results ?? []);
      if (r.status === "fulfilled") setRecommendations(r.value.data.results ?? []);
      if (i.status === "fulfilled") setIpos(i.value.data.results ?? []);
      if (s.status === "fulfilled") setSectors(s.value.data ?? []);
      setLoading(false);
    });
  };

  useEffect(() => {
    loadAll();
  }, []);

  const addStock = async (e) => {
    e.preventDefault();
    setError(null);
    if (!newSymbol.trim()) return;
    try {
      await endpoints.addStockToWatchlist(newSymbol.trim().toUpperCase());
      setNewSymbol("");
      loadAll();
    } catch {
      setError("Could not add that stock -- check the symbol and try again.");
    }
  };

  const removeStock = async (id) => {
    await endpoints.removeStockFromWatchlist(id);
    loadAll();
  };

  const recommendationFor = (stockId) => recommendations.find((r) => r.stock === stockId);

  return (
    <>
      <div className="panel">
        <h3>Stock Watchlist</h3>
        <p style={{ fontSize: 12, color: "var(--muted)" }}>
          Fundamentals are pulled and re-scored weekly (results don't change intraday) --
          add a stock here and a recommendation appears after the next sync.
        </p>

        <form onSubmit={addStock} style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <input
            placeholder="Symbol, e.g. TCS"
            value={newSymbol}
            onChange={(e) => setNewSymbol(e.target.value)}
            style={{
              background: "var(--panel)", color: "var(--text)",
              border: "1px solid var(--border)", borderRadius: 6, padding: 6, width: 160,
            }}
          />
          <button
            type="submit"
            style={{
              padding: "6px 14px", borderRadius: 6, border: "1px solid var(--accent)",
              background: "transparent", color: "var(--accent)", cursor: "pointer",
            }}
          >
            Add to watchlist
          </button>
        </form>
        {error && <p style={{ color: "var(--red)", fontSize: 12 }}>{error}</p>}

        {loading ? (
          <p style={{ color: "var(--muted)" }}>Loading…</p>
        ) : watchlist.length === 0 ? (
          <p style={{ color: "var(--muted)", fontSize: 13 }}>No stocks on your watchlist yet.</p>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                <th>Symbol</th><th>Score</th><th>Verdict</th><th>Price</th><th>Trend</th><th>Suggested Hold</th><th></th>
              </tr>
            </thead>
            <tbody>
              {watchlist.map((w) => {
                const rec = recommendationFor(w.stock);
                return (
                  <tr key={w.id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td>{w.symbol}</td>
                    <td>{rec ? `${rec.fundamental_score.toFixed(0)}/100` : "—"}</td>
                    <td style={{ color: rec ? VERDICT_COLOR[rec.verdict] : "var(--muted)" }}>
                      {rec ? VERDICT_LABEL[rec.verdict] : "Awaiting fundamentals"}
                    </td>
                    <td style={{ color: rec?.valuation_label ? VALUATION_COLOR[rec.valuation_label] : "var(--muted)" }}>
                      {rec?.valuation_label || "—"}
                    </td>
                    <td style={{ color: rec?.technical_trend ? TREND_COLOR[rec.technical_trend] : "var(--muted)" }}>
                      {rec?.technical_trend || "—"}
                    </td>
                    <td>{rec?.suggested_hold_months ? `~${rec.suggested_hold_months} months` : "—"}</td>
                    <td>
                      <button
                        onClick={() => removeStock(w.id)}
                        style={{ background: "none", border: "none", color: "var(--muted)", cursor: "pointer" }}
                      >
                        ×
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel" style={{ marginTop: 12 }}>
        <h3>Automatic Suggestions</h3>
        <p style={{ fontSize: 12, color: "var(--muted)" }}>
          Heuristic screen combining fundamentals (revenue/profit growth, growth
          consistency, ROE, ROCE, debt-to-equity, promoter holding), valuation (P/E vs the
          stock's own history), and technical trend (moving averages + RSI).
          <strong> Not investment advice</strong> — it can't see competitive
          position or risk factors. Verify results yourself before acting.
        </p>
        {recommendations.length === 0 ? (
          <p style={{ color: "var(--muted)", fontSize: 13 }}>
            No recommendations yet — add stocks to your watchlist above; fundamentals and
            scores are generated on the weekly sync.
          </p>
        ) : (
          recommendations
            .filter((r) => r.verdict === "strong_hold" || r.verdict === "moderate_hold")
            .sort((a, b) => b.fundamental_score - a.fundamental_score)
            .map((r) => (
              <div key={r.id} style={{ padding: "10px 0", borderTop: "1px solid var(--border)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 6 }}>
                  <span style={{ fontWeight: 600 }}>{r.symbol} — {r.name}</span>
                  <span style={{ fontSize: 13 }}>
                    <span style={{ color: VERDICT_COLOR[r.verdict] }}>
                      {VERDICT_LABEL[r.verdict]} · {r.fundamental_score.toFixed(0)}/100
                    </span>
                    {r.valuation_label && (
                      <span style={{ color: VALUATION_COLOR[r.valuation_label], marginLeft: 8 }}>
                        Price: {r.valuation_label}
                      </span>
                    )}
                    {r.technical_trend && (
                      <span style={{ color: TREND_COLOR[r.technical_trend], marginLeft: 8 }}>
                        {r.technical_trend}
                      </span>
                    )}
                    {r.suggested_hold_months && ` · ~${r.suggested_hold_months}mo`}
                  </span>
                </div>
                <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 4 }}>{r.reasoning}</p>
              </div>
            ))
        )}
      </div>

      <div className="panel" style={{ marginTop: 12 }}>
        <h3>Sector Breakdown</h3>
        <p style={{ fontSize: 12, color: "var(--muted)" }}>
          Groups your watchlist's latest recommendations by sector. A sector needs at least
          2 tracked stocks before "best in sector" means anything — fewer than that is one
          stock's score, not a real comparison.
        </p>
        {sectors.length === 0 ? (
          <p style={{ color: "var(--muted)", fontSize: 13 }}>No sector data yet.</p>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                <th>Sector</th><th>Avg Score</th><th>Stocks</th><th>Best in Sector</th>
              </tr>
            </thead>
            <tbody>
              {sectors.map((s) => (
                <tr key={s.sector} style={{ borderTop: "1px solid var(--border)" }}>
                  <td>{s.sector}</td>
                  <td>{s.avg_score.toFixed(0)}/100</td>
                  <td>{s.stock_count}</td>
                  <td>
                    {s.best_stock
                      ? `${s.best_stock.symbol} (${s.best_stock.score.toFixed(0)}/100, ${VERDICT_LABEL[s.best_stock.verdict]})`
                      : "— (too few tracked)"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel" style={{ marginTop: 12 }}>
        <h3>IPO Suggestions</h3>
        <p style={{ fontSize: 12, color: "var(--muted)" }}>
          A new listing has no results history to score against — this is a list of
          open/upcoming issues, not a ranking.
        </p>
        {ipos.length === 0 ? (
          <p style={{ color: "var(--muted)", fontSize: 13 }}>No IPO data synced yet.</p>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                <th>Company</th><th>Status</th><th>Opens</th><th>Closes</th><th>Price Band</th>
              </tr>
            </thead>
            <tbody>
              {ipos.map((ipo) => (
                <tr key={ipo.id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td>{ipo.company_name}</td>
                  <td>{ipo.status}</td>
                  <td>{ipo.open_date ?? "TBA"}</td>
                  <td>{ipo.close_date ?? "TBA"}</td>
                  <td>
                    {ipo.price_band_low && ipo.price_band_high
                      ? `₹${ipo.price_band_low}–₹${ipo.price_band_high}`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
