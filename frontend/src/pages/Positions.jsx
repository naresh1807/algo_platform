import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, RefreshCw, Search, TrendingUp, Wallet, PieChart } from "lucide-react";
import DataTable from "../components/DataTable.jsx";
import EmptyState from "../components/EmptyState.jsx";
import ErrorState from "../components/ErrorState.jsx";
import LoadingSkeleton from "../components/LoadingSkeleton.jsx";
import MetricCard from "../components/MetricCard.jsx";
import { endpoints } from "../services/api.js";

const money = (value) => `₹${Number(value ?? 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
export default function Positions() {
  const [positions, setPositions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState("open");
  const [mode, setMode] = useState("all");
  const load = useCallback(async (initial = false) => {
    if (initial) setLoading(true);
    try {
      const res = await endpoints.positions();
      setPositions(res.data.results ?? []);
      setError(null);
    } catch { setError("Could not load positions."); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    load(true);
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);
  const scoped = positions.filter((p) => mode === "all" || p.execution_mode === mode);
  const open = scoped.filter((p) => p.is_open);
  const closed = scoped.filter((p) => !p.is_open);
  const capital = open.reduce((sum, p) => sum + Number(p.entry_price) * p.qty, 0);
  const openPnl = open.reduce((sum, p) => sum + Number(p.unrealized_pnl), 0);
  const realized = closed.reduce((sum, p) => sum + Number(p.realized_pnl ?? p.unrealized_pnl), 0);
  const rows = (tab === "open" ? open : closed).filter((position) => `${position.symbol} ${position.side} ${position.execution_mode}`.toLowerCase().includes(query.toLowerCase()));
  const columns = [
    { key: "symbol", label: "Instrument", render: (p) => <div className="table-instrument"><strong>{p.symbol}</strong><small>{p.option_contract ? "Option" : "Underlying"} · {p.execution_mode.toUpperCase()}</small></div> },
    { key: "side", label: "Side", render: (p) => <span className={`order-side ${p.side === "long" ? "buy" : "sell"}`}>{p.side.toUpperCase()}</span> },
    { key: "qty", label: "Qty", align: "right" },
    { key: "entry_price", label: "Avg. entry", align: "right", render: (p) => money(p.entry_price) },
    { key: "mark", label: tab === "open" ? "Last mark" : "Exit", align: "right", render: (p) => (tab === "open" ? p.last_mark_price : p.exit_price) == null ? "—" : money(tab === "open" ? p.last_mark_price : p.exit_price) },
    { key: "stop_loss", label: "Stop / target", render: (p) => <div className="table-instrument"><span>{money(p.stop_loss)} / {p.target_price == null ? "—" : money(p.target_price)}</span>{p.trailing_stop_distance != null && <small>Trailing stop enabled</small>}</div> },
    { key: "unrealized_pnl", label: tab === "open" ? "Net unrealized P&L" : "Net realized P&L", align: "right", render: (p) => {
      const value = tab === "open" ? p.unrealized_pnl : p.realized_pnl ?? p.unrealized_pnl;
      return <strong className={Number(value) >= 0 ? "num-positive" : "num-negative"}>{money(value)}</strong>;
    } },
    { key: "opened_at", label: tab === "open" ? "Opened" : "Closed", render: (p) => new Date(tab === "open" ? p.opened_at : p.closed_at).toLocaleString("en-IN") },
    { key: "trade", label: "", sortable: false, render: (p) => p.is_open && p.option_contract ? <Link className="portfolio-exit-link" to="/option-trading">Manage / exit <ArrowUpRight size={13} /></Link> : null },
  ];
  return <div className="portfolio-page">
    <div className="portfolio-summary">
      <MetricCard title="Open positions" value={error ? null : open.length} icon={Wallet} loading={loading} />
      <MetricCard title="Entry capital deployed" value={error ? null : money(capital)} icon={PieChart} loading={loading} />
      <MetricCard title="Open net P&L" value={error ? null : money(openPnl)} icon={TrendingUp} tone={openPnl >= 0 ? "positive" : "negative"} loading={loading} />
      <MetricCard title="Closed net P&L" value={error ? null : money(realized)} icon={TrendingUp} tone={realized >= 0 ? "positive" : "negative"} loading={loading} />
    </div>
    <div className="panel">
      <div className="orders-toolbar"><div className="segmented-tabs" role="group" aria-label="Position status">
        <button className={tab === "open" ? "active" : ""} onClick={() => setTab("open")} aria-pressed={tab === "open"}>Open ({open.length})</button>
        <button className={tab === "closed" ? "active" : ""} onClick={() => setTab("closed")} aria-pressed={tab === "closed"}>Closed ({closed.length})</button>
      </div><div className="portfolio-controls"><select className="input" aria-label="Filter execution mode" value={mode} onChange={(event) => setMode(event.target.value)}><option value="all">All modes</option><option value="paper">Paper</option><option value="live">Live</option></select>
        <button className="icon-btn" onClick={() => load(true)} aria-label="Refresh positions"><RefreshCw size={17} /></button>
      </div></div>
      <label className="record-search"><Search size={16} /><input type="search" placeholder="Search your positions?" aria-label="Search positions" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <div className="desktop-records"><DataTable columns={columns} rows={rows} getRowKey={(p) => p.id} searchKeys={["symbol", "side", "execution_mode"]} searchPlaceholder="Search your positions…" loading={loading} error={error} onRetry={() => load(true)} emptyTitle={`No ${tab} positions`} emptyDetail={tab === "open" ? "Your open trading positions will appear here." : "Positions appear here after they are closed."} /></div>
      <div className="mobile-records">{loading ? <LoadingSkeleton rows={3} /> : error ? <ErrorState detail={error} onRetry={() => load(true)} /> : rows.length === 0 ? <EmptyState title={`No ${tab} positions`} detail={tab === "open" ? "Your open trading positions will appear here." : "Positions appear here after they are closed."} /> : rows.map((position) => {
        const pnl = position.is_open ? position.unrealized_pnl : position.realized_pnl ?? position.unrealized_pnl;
        const price = position.is_open ? position.last_mark_price : position.exit_price;
        return <article className="mobile-record-card" key={position.id}>
          <div className="mobile-record-heading"><strong>{position.symbol}</strong><span className={Number(pnl) >= 0 ? "num-positive" : "num-negative"}>{money(pnl)}</span></div>
          <div className="mobile-record-subline"><small>{position.execution_mode.toUpperCase()} ? {position.side.toUpperCase()}</small><small>{position.is_open ? "Net unrealized P&L" : "Net realized P&L"}</small></div>
          <div className="mobile-record-values"><span>Quantity<strong>{position.qty}</strong></span><span>Avg. entry<strong>{money(position.entry_price)}</strong></span><span>{position.is_open ? "Last mark" : "Exit"}<strong>{price == null ? "?" : money(price)}</strong></span></div>
          <div className="mobile-position-footer"><span>SL {money(position.stop_loss)} ? Target {position.target_price == null ? "?" : money(position.target_price)}</span>{position.is_open && position.option_contract && <Link to="/option-trading">Manage / exit <ArrowUpRight size={12} /></Link>}</div>
        </article>;
      })}</div>
      <p className="orders-footnote">Trading positions from the latest 500 records. Summary values reflect the selected mode and loaded records.</p>
    </div>
  </div>;
}
