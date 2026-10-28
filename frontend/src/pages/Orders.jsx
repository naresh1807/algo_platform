import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, RefreshCw, Search } from "lucide-react";
import DataTable from "../components/DataTable.jsx";
import EmptyState from "../components/EmptyState.jsx";
import ErrorState from "../components/ErrorState.jsx";
import LoadingSkeleton from "../components/LoadingSkeleton.jsx";
import { endpoints } from "../services/api.js";

const formatPremium = (value) => value == null ? "?" : `?${Number(value).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
const FILTERS = [
  { id: "all", label: "All orders" }, { id: "active", label: "In progress" },
  { id: "executed", label: "Completed" }, { id: "rejected", label: "Rejected" },
];
export default function Orders() {
  const [orders, setOrders] = useState([]);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const load = useCallback(async (initial = false) => {
    if (initial) setLoading(true);
    try {
      const res = await endpoints.optionOrders();
      setOrders((res.data.results ?? []).map((order) => ({ ...order, contract: order.position?.symbol ?? (order.signal_id ? `Signal #${order.signal_id}` : "Option request") })));
      setError(null);
    } catch { setError("Could not load your option orders. Try refreshing."); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    load(true);
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);
  const visible = orders.filter((order) => (filter === "all" || (filter === "active" ? ["unknown", "processing"].includes(order.status) : order.status === filter)) && `${order.contract} ${order.request_id} ${order.message} ${order.status}`.toLowerCase().includes(query.toLowerCase()));
  const columns = [
    { key: "contract", label: "Contract / request", render: (o) => <div className="table-instrument"><strong>{o.position?.symbol ?? (o.signal_id ? `Signal #${o.signal_id}` : "Option request")}</strong><small>{o.request_id}</small></div> },
    { key: "action", label: "Side", render: (o) => <span className={`order-side ${o.action}`}>{o.action === "buy" ? "BUY" : "SELL TO CLOSE"}</span> },
    { key: "status", label: "Status", render: (o) => <span className={`order-status ${o.status}`}>{o.status === "unknown" ? "Needs reconciliation" : o.status}</span> },
    { key: "execution_mode", label: "Mode", render: (o) => o.execution_mode.toUpperCase() },
    { key: "quantity", label: "Qty", render: (o) => o.position?.qty ?? "—" },
    { key: "created_at", label: "Placed", render: (o) => new Date(o.created_at).toLocaleString("en-IN") },
    { key: "message", label: "Details" },
  ];
  return <div className="orders-page">
    <div className="panel orders-panel">
      <div className="orders-toolbar"><div className="segmented-tabs" role="group" aria-label="Filter orders">
        {FILTERS.map((item) => <button key={item.id} className={filter === item.id ? "active" : ""} onClick={() => setFilter(item.id)} aria-pressed={filter === item.id}>{item.label}</button>)}
      </div><button className="icon-btn" onClick={() => load(true)} aria-label="Refresh orders"><RefreshCw size={17} /></button></div>
      <label className="record-search"><Search size={16} /><input type="search" placeholder="Search contract or order?" aria-label="Search orders" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <div className="desktop-records"><DataTable rows={visible} columns={columns} getRowKey={(o) => o.request_id} searchKeys={["request_id", "message", "status"]} searchPlaceholder="Search your orders…" loading={loading} error={error} onRetry={() => load(true)} emptyTitle="No orders in this view" emptyDetail="Your manual option orders appear here as you trade." /></div>
      <div className="mobile-records">{loading ? <LoadingSkeleton rows={3} /> : error ? <ErrorState detail={error} onRetry={() => load(true)} /> : visible.length === 0 ? <EmptyState title="No orders in this view" detail="Your manual option orders appear here as you trade." /> : visible.map((order) => (
        <article className="mobile-record-card" key={order.request_id}>
          <div className="mobile-record-heading"><strong>{order.contract}</strong><span className={`order-side ${order.action}`}>{order.action === "buy" ? "BUY" : "SELL"}</span></div>
          <div className="mobile-record-subline"><span className={`order-status ${order.status}`}>{order.status === "unknown" ? "Needs reconciliation" : order.status}</span><small>{order.execution_mode.toUpperCase()}</small></div>
          <div className="mobile-record-values"><span>Quantity<strong>{order.position?.qty ?? "?"}</strong></span><span>Filled premium<strong>{formatPremium(order.action === "buy" ? order.position?.entry_price : order.position?.exit_price)}</strong></span><span>Placed<strong>{new Date(order.created_at).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}</strong></span></div>
          <details className="mobile-order-detail"><summary>Order details</summary><p>{order.message}</p><small>{new Date(order.created_at).toLocaleString("en-IN")}</small><small>Request: {order.request_id}</small></details>
        </article>
      ))}</div>
      <div className="orders-footnote">Your latest 100 manual option requests. <Link to="/signals">View automated signals <ArrowUpRight size={12} /></Link></div>
    </div>
    <Link className="mobile-new-order btn btn-primary" to="/option-trading">Place an option order <ArrowUpRight size={17} /></Link>
  </div>;
}
