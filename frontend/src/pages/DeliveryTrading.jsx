import { useCallback, useEffect, useRef, useState } from "react";
import { endpoints } from "../services/api.js";
import { newOrderRequestId } from "../utils/orderRequest.js";
import AppDialog from "../components/AppDialog.jsx";

const money = value => Number(value ?? 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const describeError = error => error.response?.data?.error || "Could not connect to the trading server. Try again.";
const pendingKey = "delivery-pending-order";
const readPending = () => { try { return JSON.parse(sessionStorage.getItem(pendingKey) || "null"); } catch { return null; } };
export default function DeliveryTrading() {
  const [account, setAccount] = useState(null);
  const [stocks, setStocks] = useState([]);
  const [watchlist, setWatchlist] = useState([]);
  const [orders, setOrders] = useState([]);
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState("discover");
  const [ticket, setTicket] = useState(null);
  const [quantity, setQuantity] = useState("1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(readPending);
  const inFlight = useRef(false);
  const refresh = useCallback(async () => {
    try {
      const [a, o, w] = await Promise.all([endpoints.deliveryAccount(), endpoints.deliveryOrders(), endpoints.stockWatchlist()]);
      setAccount(a.data); setOrders(o.data.results); setWatchlist(w.data.results ?? []);
    } catch (err) { setError(describeError(err)); }
  }, []);
  useEffect(() => { refresh(); const timer = setInterval(refresh, 10000); return () => clearInterval(timer); }, [refresh]);
  useEffect(() => {
    let active = true;
    const timer = setTimeout(() => endpoints.deliveryStocks(search, tab === "watchlist").then(res => { if (active) setStocks(res.data.results); }).catch(err => { if (active) setError(describeError(err)); }), 250);
    return () => { active = false; clearTimeout(timer); };
  }, [search, tab, watchlist]);
  const openTicket = (stock, action) => { if (pending || busy) return; setQuantity("1"); setTicket({ stock, action }); setError(""); };
  const place = async payload => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(""); setMessage("");
    setPending(payload); sessionStorage.setItem(pendingKey, JSON.stringify(payload));
    try {
      const res = await endpoints.placeDeliveryOrder(payload);
      setMessage(`${res.data.action.toUpperCase()} ${res.data.quantity} ${res.data.symbol} completed in PAPER mode.`);
      setPending(null); sessionStorage.removeItem(pendingKey); setTicket(null);
    } catch (err) {
      setError(describeError(err));
      if (err.response && err.response.status < 500) { setPending(null); sessionStorage.removeItem(pendingKey); }
    } finally { await refresh(); setBusy(false); inFlight.current = false; }
  };
  const toggleWatch = async stock => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true);
    try {
      const item = watchlist.find(w => w.stock === stock.id);
      if (item) await endpoints.removeStockFromWatchlist(item.id);
      else await endpoints.addStockToWatchlist(stock.symbol);
      await refresh();
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); inFlight.current = false; }
  };
  const visibleStocks = tab === "watchlist" ? stocks.filter(s => watchlist.some(w => w.stock === s.id)) : stocks;
  const validQuantity = Number.isInteger(Number(quantity)) && Number(quantity) > 0 && Number(quantity) <= 100000;
  return <div className="delivery-workspace">
    <div className="panel delivery-mode"><span className="badge badge-accent">PAPER DELIVERY</span><p>Independent simulated account with INR 100,000 starting cash. Trades use the latest stored daily close, dated below. Fees are excluded. Live delivery is not enabled.</p></div>
    {error && <p className="num-negative" role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {pending && <div className="panel"><p>A request is awaiting a result. Check the same request before placing another order.</p><button className="btn" disabled={busy} onClick={() => place(pending)}>Check / retry same order</button></div>}
    <div className="delivery-summary">
      {[ ["Paper cash", account?.cash], ["Holdings value", account?.holdings_value], ["Portfolio value", account?.portfolio_value], ["Realized P&L", account?.realized_pnl] ].map(([label, value]) => <div className="panel" key={label}><small>{label}</small><strong>{value == null ? "Loading..." : `INR ${money(value)}`}</strong></div>)}
    </div>
    <div className="panel"><div className="segmented-tabs" aria-label="Delivery views">{[ ["discover", "Stocks"], ["watchlist", "Watchlist"], ["holdings", "Holdings"], ["orders", "Orders"] ].map(([value, label]) => <button className="btn" key={value} aria-pressed={tab === value} onClick={() => setTab(value)}>{label}</button>)}</div>
      {["discover", "watchlist"].includes(tab) && <><label className="delivery-search">Search stocks<input className="input" type="search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Symbol or company name" /></label><p>Showing up to 30 matching stocks from the platform catalog.</p><div className="delivery-records">{visibleStocks.map(stock => <article className="delivery-record" key={stock.id}><div><strong>{stock.symbol}</strong><p>{stock.name} / {stock.exchange}</p><small>Daily close: {stock.price_date ?? "Unavailable"}</small></div><strong>{stock.price == null ? "No price" : `INR ${money(stock.price)}`}</strong><div className="delivery-buttons"><button className="btn" disabled={busy} onClick={() => toggleWatch(stock)}>{watchlist.some(w => w.stock === stock.id) ? "Remove star" : "Star"}</button><button className="btn chart-buy" disabled={busy || !!pending || !stock.price} onClick={() => openTicket(stock, "buy")}>Buy</button></div></article>)}</div>{!visibleStocks.length && <p>No matching stocks. Try a different search or add a stock to your watchlist.</p>}</>}
      {tab === "holdings" && <div className="delivery-records">{(account?.holdings ?? []).map(stock => <article className="delivery-record" key={stock.id}><div><strong>{stock.symbol}</strong><p>{stock.quantity} shares / Average INR {money(stock.average_price)}</p><small>Mark date: {stock.price_date ?? "Unavailable"}</small></div><strong className={Number(stock.unrealized_pnl) < 0 ? "num-negative" : "num-positive"}>INR {money(stock.unrealized_pnl)}</strong><div className="delivery-buttons"><button className="btn chart-buy" disabled={busy || !!pending} onClick={() => openTicket(stock, "buy")}>Buy more</button><button className="btn btn-danger" disabled={busy || !!pending} onClick={() => openTicket(stock, "sell")}>Sell</button></div></article>)}{!account?.holdings?.length && <p>No delivery holdings yet.</p>}</div>}
      {tab === "orders" && <div className="delivery-records">{orders.map(order => <article className="delivery-record" key={order.request_id}><div><strong>{order.action.toUpperCase()} {order.symbol}</strong><p>{order.quantity} shares at INR {money(order.price)}</p><small>{new Date(order.created_at).toLocaleString("en-IN")} / Reference: {order.reference_date}</small></div><span className="badge badge-accent">PAPER EXECUTED</span></article>)}{!orders.length && <p>No delivery orders yet.</p>}</div>}
    </div>
    <AppDialog open={!!ticket} onClose={() => { if (!busy) setTicket(null); }} label="Review delivery order" className="delivery-dialog">
      {ticket && <form onSubmit={e => { e.preventDefault(); place({ request_id: newOrderRequestId(), stock_id: ticket.stock.id, action: ticket.action, quantity: Number(quantity) }); }}><h2>{ticket.action.toUpperCase()} {ticket.stock.symbol}</h2><p>Paper delivery / daily-close simulation</p><label>Shares<input className="input" type="number" min="1" max={ticket.action === "sell" ? ticket.stock.quantity : 100000} step="1" required disabled={busy || !!pending} value={quantity} onChange={e => setQuantity(e.target.value)} /></label><dl className="option-order-preview"><dt>Reference price</dt><dd>INR {money(ticket.stock.price)}</dd><dt>Price date</dt><dd>{ticket.stock.price_date}</dd><dt>Estimated value</dt><dd>INR {money(Number(quantity) * Number(ticket.stock.price))}</dd></dl><p>The server rechecks price, cash and holdings when confirming. This submits no real broker order.</p><div className="delivery-buttons"><button type="button" className="btn" disabled={busy} onClick={() => setTicket(null)}>Cancel</button><button className={`btn ${ticket.action === "buy" ? "chart-buy" : "btn-danger"}`} disabled={busy || !!pending || !validQuantity}>Confirm paper {ticket.action}</button></div></form>}
    </AppDialog>
  </div>;
}
