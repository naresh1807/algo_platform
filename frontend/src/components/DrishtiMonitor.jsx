import { useEffect, useState } from "react";
import { endpoints } from "../services/api.js";
import PriceChart from "../charts/PriceChart.jsx";
import { useThemeStore } from "../store/themeStore.js";
const stamp = value => new Date(value).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour:"2-digit", minute:"2-digit" });
const money = value => `INR ${Number(value).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
const label = value => String(value).replaceAll("_", " ");
export function istDay(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-GB", {timeZone:"Asia/Kolkata",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(now);
  const get = type => parts.find(p => p.type === type).value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}
function Evidence({ evidence }) {
  if (!evidence) return <p className="drishti-muted">No entry evidence recorded.</p>;
  return <div className="drishti-evidence">{["positive_factors","negative_factors"].map(key => evidence[key]?.length ? <div key={key}><h4>{key === "positive_factors" ? "Supporting factors" : "Opposing factors"}</h4>{evidence[key].map((f,i) => <div className="drishti-factor" key={i}><span>{label(f.feature ?? "Factor")}</span><strong>{Number(f.contribution).toFixed(3)}</strong></div>)}</div> : null)}{evidence.method && <p className="drishti-muted">Analysis: {label(evidence.method)}</p>}</div>;
}
function Metric({ title, value }) { return <div className="drishti-metric"><small>{title}</small><strong>{value}</strong></div>; }
function JournalEntry({ trade }) {
  const [chart,setChart] = useState(null); const [error,setError] = useState("");
  const theme = useThemeStore(s => s.theme);
  const load = event => {if(event.currentTarget.open && chart === null) endpoints.paperJournalChart(trade.id).then(r => {setChart(r.data.results);setError("");}).catch(() => setError("Chart history could not be loaded."));};
  return <article className="drishti-card"><div className="drishti-card-header"><div><span className="drishti-eyebrow">CLOSED PAPER TRADE</span><h3>{trade.contract}</h3><small>{stamp(trade.opened_at)} - {stamp(trade.closed_at)} IST</small></div><strong className={Number(trade.net_pnl)>=0 ? "positive" : "negative"}>{money(trade.net_pnl)}</strong></div>
    <div className="drishti-metrics"><Metric title="Entry" value={money(trade.entry_price)}/><Metric title="Exit" value={money(trade.exit_price)}/><Metric title="Quantity" value={trade.quantity}/><Metric title="Initial stop" value={money(trade.stop)}/><Metric title="Gross P&L" value={money(trade.gross_pnl)}/><Metric title="Costs" value={money(trade.costs)}/></div>
    <p className="drishti-reason">Exit: {label(trade.exit_reason ?? "Not recorded")}</p><span className="badge badge-accent">{trade.learning_status}</span>
    <details className="drishti-details" onToggle={load}><summary>Entry analysis and contract chart</summary><p className="drishti-muted">Entry model: {trade.model_version ?? "Not recorded"}</p><Evidence evidence={trade.entry_evidence}/>{error ? <p role="alert">{error}</p> : chart === null ? <p>Loading contract history...</p> : chart.length ? <PriceChart candles={chart} theme={theme}/> : <p>No stored candles for this trade window.</p>}</details>
  </article>;
}
export default function DrishtiMonitor() {
  const [data,setData] = useState(null); const [error,setError] = useState(""); const [day,setDay] = useState(istDay);
  useEffect(() => {let active=true;const load=()=>endpoints.drishtiMonitoring().then(r=>{if(active){setData(r.data);setError("");}}).catch(()=>{if(active)setError("Could not refresh Drishti monitoring.");});load();const timer=setInterval(load,30000);return()=>{active=false;clearInterval(timer);};},[day]);
  useEffect(()=>{const tick=()=>setDay(istDay());const timer=setInterval(tick,1000);window.addEventListener("focus",tick);return()=>{clearInterval(timer);window.removeEventListener("focus",tick);};},[]);
  const today = data?.date === day ? data : null;
  return <div className="drishti-workspace"><section className="panel"><div className="drishti-section-heading"><div><span className="drishti-eyebrow">DRISHTI LIVE ACTIVITY</span><h2>Today's decisions</h2></div><span className="badge badge-accent">{day} IST</span></div><p className="drishti-muted">Today's cards clear at 12:00 AM IST. Trade records remain saved for learning.</p>{error && <p role="alert">{error}</p>}
    <div className="drishti-metrics"><Metric title="Execution signals" value={today?.execution_signal_count ?? 0}/><Metric title="Rejected signals" value={today?.rejected_signal_count ?? 0}/>{Object.entries(today?.decision_actions ?? {}).map(([action,count])=><Metric key={action} title={label(action)} value={count}/>)}</div>
    <h3>Entry blockers</h3><div className="drishti-grid">{Object.entries(today?.blockers ?? {}).map(([reason,count])=><article className="drishti-card drishti-blocker" key={reason}><span>{reason}</span><strong>{count}</strong><small>Rejected execution signals</small></article>)}</div>{today && !Object.keys(today.blockers).length && <p className="drishti-muted">No rejected execution signals today.</p>}
    <h3>Paper-AI decisions</h3><div className="drishti-grid">{today?.decisions.map(d=><article className="drishti-card" key={d.id}><div className="drishti-card-header"><h3>{d.symbol}</h3><span className="badge badge-accent">{label(d.action)}</span></div><small>{stamp(d.timestamp)} IST</small><p className="drishti-reason">{d.risk_engine_response_json?.reason ?? "No risk rejection recorded"}</p><div className="drishti-metrics"><Metric title="Confidence" value={d.confidence == null ? "Unavailable" : `${(Number(d.confidence)*100).toFixed(1)}%`}/></div><details className="drishti-details"><summary>View decision factors</summary><p className="drishti-muted">Model: {d.model_version || "Not recorded"}</p><Evidence evidence={d.explanation_json}/></details></article>)}</div>{!today?.decisions.length && <p className="drishti-muted">No decisions available for today.</p>}
  </section><section className="panel"><div className="drishti-section-heading"><h2>Today's paper-trade journal</h2><span className="badge badge-accent">{today?.journal.length ?? 0} completed</span></div><p className="drishti-muted">Collection records the outcome for learning. It does not mean a new model has been trained.</p><div className="drishti-grid">{today?.journal.map(t=><JournalEntry key={t.id} trade={t}/>)}</div>{!today?.journal.length && <p className="drishti-muted">No completed paper trades today.</p>}</section></div>;
}
