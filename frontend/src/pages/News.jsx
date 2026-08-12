import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";

const SYMBOL_OPTIONS = [
  { value: "", label: "All symbols" },
  { value: "NIFTY", label: "NIFTY" },
  { value: "BANKNIFTY", label: "BANKNIFTY" },
];

const SENTIMENT_OPTIONS = [
  { value: "", label: "All sentiment" },
  { value: "positive", label: "Positive" },
  { value: "neutral", label: "Neutral" },
  { value: "negative", label: "Negative" },
];

// Mirrors apps.news.models.NewsImpactAnalysis.TradeRelevance exactly --
// this is client-side-only filtering (the trade_relevance value lives
// on the nested impact_analysis object NewsSentimentSerializer already
// returns, not a queryable field on the sentiment endpoint itself), so
// there's no matching API param for it the way symbol/sentiment_label
// have.
const RELEVANCE_OPTIONS = [
  { value: "", label: "All relevance" },
  { value: "direct", label: "Direct (named stock)" },
  { value: "sector", label: "Sector-wide" },
  { value: "macro", label: "Macro / index-wide" },
  { value: "none", label: "No clear relevance" },
];

const selectStyle = {
  background: "var(--panel)", color: "var(--text)",
  border: "1px solid var(--border)", borderRadius: 6, padding: 6,
};

const REFRESH_INTERVAL_MS = 60_000; // apps.news.tasks.poll_news_sources runs every 5 min via Celery beat -- polling more often than that just re-shows the same rows, but a full 5-minute wait to notice fresh news would feel stale for a page whose whole point is "what's new."

function formatIst(isoString) {
  return new Date(isoString).toLocaleString("en-GB", {
    timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }) + " IST";
}

/**
 * apps.news -- real headlines (apps.news.rss_client: public RSS feeds +
 * Google News search, no paid API) scored by FinBERT and enriched with
 * entity/sector/trade-relevance extraction (apps.news.impact_engine).
 * Didn't exist as a page until now -- sentiment data was only ever
 * surfaced as a single rolled-up summary line on the Dashboard
 * (endpoints.marketSummary), with no way to see the actual headlines
 * driving it.
 */
export default function News() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [symbol, setSymbol] = useState("");
  const [sentimentLabel, setSentimentLabel] = useState("");
  const [relevance, setRelevance] = useState("");
  const [marketSummary, setMarketSummary] = useState(null);

  const load = () => {
    setError(null);
    endpoints.newsSentiment(symbol || undefined, sentimentLabel || undefined)
      .then((res) => {
        setItems(res.data.results ?? []);
        setLoading(false);
      })
      .catch(() => {
        setError("Could not load news -- is the backend running and BROKER_MODE/Celery beat active?");
        setLoading(false);
      });
  };

  useEffect(() => {
    endpoints.marketSummary().then((res) => setMarketSummary(res.data.summary)).catch(() => {});
  }, []);

  useEffect(() => {
    setLoading(true);
    load();
    const interval = setInterval(load, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, sentimentLabel]);

  // General market feeds (apps.news.rss_sources.GENERAL_MARKET_FEEDS)
  // are deliberately included for EVERY watchlist symbol -- broad
  // market news is relevant whether you're looking at NIFTY or
  // BANKNIFTY, see that module's docstring. That means the same real
  // article gets its own NewsSentiment row per symbol, which is correct
  // for per-symbol filtered views but reads as duplicate spam on the
  // combined "All symbols" view -- merge those back into one row here
  // (client-side only; the underlying rows are intentionally separate,
  // this is purely a display collapse), keeping every symbol it was
  // tagged under as a combined label.
  const relevanceFiltered = relevance
    ? items.filter((n) => (n.impact_analysis?.trade_relevance ?? "none") === relevance)
    : items;

  const filtered = symbol
    ? relevanceFiltered
    : Object.values(
        relevanceFiltered.reduce((acc, n) => {
          // published_at included: the same real article tagged under
          // multiple symbols shares an identical published_at (it's the
          // same ingested row, duplicated per symbol -- see the comment
          // above), so this still merges correctly. Without it, two
          // genuinely distinct stories that happen to share a headline
          // and source (a common wire-service pattern) but ran at
          // different times could incorrectly merge into one row.
          const key = `${n.headline}|${n.source}|${n.published_at}`;
          if (!acc[key]) {
            acc[key] = { ...n, symbols: [n.symbol] };
          } else if (!acc[key].symbols.includes(n.symbol)) {
            acc[key].symbols.push(n.symbol);
          }
          return acc;
        }, {})
      ).sort((a, b) => new Date(b.published_at) - new Date(a.published_at));

  return (
    <div>
      {marketSummary && (
        <div className="panel" style={{ marginBottom: 12 }}>
          <h3>Today's Market Summary</h3>
          <p style={{ margin: 0 }}>{marketSummary}</p>
        </div>
      )}

      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <h3 style={{ margin: 0 }}>News</h3>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={selectStyle}>
              {SYMBOL_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select value={sentimentLabel} onChange={(e) => setSentimentLabel(e.target.value)} style={selectStyle}>
              {SENTIMENT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select value={relevance} onChange={(e) => setRelevance(e.target.value)} style={selectStyle}>
              {RELEVANCE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <button
              onClick={load}
              style={{
                padding: "6px 14px", borderRadius: 6, border: "1px solid var(--accent)",
                background: "transparent", color: "var(--accent)", cursor: "pointer",
              }}
            >
              Refresh
            </button>
          </div>
        </div>

        {error && <p style={{ color: "var(--red)", marginTop: 12 }}>{error}</p>}
        {loading && !error && <p style={{ color: "var(--muted)", marginTop: 12 }}>Loading…</p>}

        {!loading && !error && filtered.length === 0 && (
          <p style={{ color: "var(--muted)", marginTop: 12 }}>
            No headlines yet -- apps.news.tasks.poll_news_sources runs every 5 minutes via Celery beat
            once the worker is up. If this stays empty, check the worker log for rss_client warnings.
          </p>
        )}

        {!loading && !error && filtered.length > 0 && (
          <div className="card-grid">
            {filtered.map((n) => {
              const impact = n.impact_analysis;
              const accent = n.sentiment_label === "positive" ? "green" : n.sentiment_label === "negative" ? "red" : "muted";
              const badgeClass = n.sentiment_label === "positive" ? "badge-green" : n.sentiment_label === "negative" ? "badge-red" : "badge-muted";
              return (
                <div key={n.id} className={`card card-accent-${accent}`}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "flex-start" }}>
                    <div style={{ fontSize: 14, fontWeight: 500 }}>{n.headline}</div>
                    <span className={`badge ${badgeClass}`} style={{ flexShrink: 0 }}>
                      {n.sentiment_label} {n.sentiment_score != null ? `(${n.sentiment_score.toFixed(2)})` : ""}
                    </span>
                  </div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 8, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                    <span>{n.source}</span>
                    <span>{formatIst(n.published_at)}</span>
                    {n.symbols ? (
                      <span>Tagged: {n.symbols.join(", ")}</span>
                    ) : (
                      n.symbol && <span>Tagged: {n.symbol}</span>
                    )}
                    {impact && impact.trade_relevance !== "none" && (
                      <span className="badge badge-accent">
                        {RELEVANCE_OPTIONS.find((o) => o.value === impact.trade_relevance)?.label ?? impact.trade_relevance}
                      </span>
                    )}
                    {impact?.sectors?.length > 0 && <span>Sectors: {impact.sectors.join(", ")}</span>}
                    {impact?.likely_rising_stocks?.length > 0 && (
                      <span style={{ color: "var(--green)" }}>▲ {impact.likely_rising_stocks.join(", ")}</span>
                    )}
                    {impact?.likely_falling_stocks?.length > 0 && (
                      <span style={{ color: "var(--red)" }}>▼ {impact.likely_falling_stocks.join(", ")}</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
