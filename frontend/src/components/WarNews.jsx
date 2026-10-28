import { useEffect, useState } from "react";
import { endpoints } from "../services/api.js";
export default function WarNews() {
  const [feed, setFeed] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    setBusy(true); setError("");
    try { const res = await endpoints.warHeadlines(); setFeed(res.data); }
    catch { setError("Could not fetch war news. Please try again."); }
    finally { setBusy(false); }
  };
  useEffect(() => { load(); const timer = setInterval(load, 300000); return () => clearInterval(timer); }, []);
  return <div className="panel"><div className="option-trading-heading"><h3>War & geopolitical news</h3><button className="btn" disabled={busy} onClick={load}>{busy ? "Loading..." : "Refresh"}</button></div>
    <p>Published in the last 24 hours. Raw sourced headlines; no sentiment score or trading instruction.</p>
    {error && <p role="alert">{error}</p>}
    {feed && <small>Feed checked: {new Date(feed.fetched_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST</small>}
    {feed?.results?.map((item, i) => <article className="delivery-record" key={`${item.url}:${i}`}><div><a href={item.url} target="_blank" rel="noopener noreferrer">{item.headline}</a><p>{item.source} / {new Date(item.published_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST</p></div></article>)}
    {feed && !feed.results.length && <p>No matching headlines were returned. The source may be temporarily unavailable.</p>}
  </div>;
}
