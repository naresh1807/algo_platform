import { useEffect, useState } from "react";

import { endpoints } from "../services/api.js";

const BIAS_COLOR = {
  Bullish: "var(--green)",
  Bearish: "var(--red)",
  Neutral: "var(--muted)",
};

/**
 * apps.jarvis.market_intelligence -- "మన వెబ్సైట్లో పూర్తిగా ML జార్విస్
 * పైన డిపెండ్ అయ్యే వర్క్ అవుతుంది... ఇది ఎప్పటికప్పుడు అన్ని స్టాక్స్
 * ఆప్షన్స్ ఇండెక్స్ పైన మరియు న్యూస్ పైన అబ్జర్వ్ చేసి మార్కెట్ ఏ
 * విధంగా ఉండబోతుందో విశ్లేషించి" -- the trader-requested single
 * synthesized "what does the market look like, what's worth buying"
 * report, refreshed every 15 minutes during market hours
 * (apps.jarvis.tasks.generate_market_outlook).
 *
 * IMPORTANT: this panel is a rollup of OTHER modules' own outputs
 * (apps.signals, apps.news, apps.options, apps.investing) explained
 * together -- not a new, separate prediction. The summary text itself
 * always says so, and this panel deliberately shows the full text
 * rather than just a headline number, so the reasoning stays visible.
 */
// Matches the backend's own refresh cadence
// (apps.jarvis.tasks.generate_market_outlook, every 15 min during
// market hours) -- this panel previously fetched once on mount only,
// so a trader who left the Dashboard open all day kept seeing the
// outlook from whenever the page first loaded, despite the "refreshed
// every 15 min" text right below it.
const REFRESH_INTERVAL_MS = 15 * 60 * 1000;

export default function MarketOutlookPanel() {
  const [outlook, setOutlook] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchOutlook = () => {
      endpoints.marketOutlook().then((res) => {
        setOutlook(res.data);
        setLoading(false);
      });
    };
    fetchOutlook();
    const id = setInterval(fetchOutlook, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);

  if (loading) return null;

  if (!outlook) {
    // Previously returned null here too -- the panel just silently
    // never appeared, with no clue why, unlike every other panel on
    // this dashboard (Performance, Daily Review, etc.) which all show
    // an explanatory empty state. generate_market_outlook only runs
    // every 15 min during market hours, so this is a real, common
    // window (pre-market, after-hours, or the first 15 min post-open).
    return (
      <div className="panel" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: "0 0 8px" }}>Market Outlook</h3>
        <p style={{ fontSize: 13, color: "var(--muted)", margin: 0 }}>
          Not generated yet -- runs every 15 minutes during market hours (apps.jarvis.tasks.generate_market_outlook).
        </p>
      </div>
    );
  }

  const biasColor = BIAS_COLOR[outlook.bias] || "var(--muted)";

  return (
    <div className="panel" style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
        <h3 style={{ margin: 0 }}>Market Outlook</h3>
        <span style={{ color: biasColor, fontWeight: 600, fontSize: 16 }}>
          {outlook.bias}
          {outlook.bias_confidence != null && (
            <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 400 }}>
              {" "}({(outlook.bias_confidence * 100).toFixed(0)}% confidence)
            </span>
          )}
        </span>
      </div>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", margin: "10px 0" }}>
        {outlook.best_options_idea && (
          <div style={{ fontSize: 13 }}>
            <span style={{ color: "var(--muted)" }}>Options idea: </span>
            <strong>{outlook.best_options_idea}</strong>
          </div>
        )}
        {outlook.best_stock_idea && (
          <div style={{ fontSize: 13 }}>
            <span style={{ color: "var(--muted)" }}>Long-term stock idea: </span>
            <strong>{outlook.best_stock_idea}</strong>
          </div>
        )}
      </div>

      <p style={{ fontSize: 13, color: "var(--text)", lineHeight: 1.5 }}>{outlook.summary}</p>

      {outlook.generated_at && (
        <p style={{ fontSize: 11, color: "var(--muted)", marginTop: 6 }}>
          As of {new Date(outlook.generated_at).toLocaleString()} — refreshed every 15 min during market hours.
        </p>
      )}
    </div>
  );
}
