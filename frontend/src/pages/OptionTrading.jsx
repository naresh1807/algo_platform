import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import OptionContractChartModal from "../components/OptionContractChartModal.jsx";
import DataTable from "../components/DataTable.jsx";
import OptionChainTable from "../components/OptionChainTable.jsx";
import { useExpiryStatus } from "../hooks/useExpiryStatus.js";
import { endpoints } from "../services/api.js";
import { newOrderRequestId, readPendingOrder, rememberPendingOrder } from "../utils/orderRequest.js";

const money = (value) => Number(value ?? 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const errorText = (error) => {
  const body = error.response?.data;
  return body?.error || body?.message || (body ? JSON.stringify(body) : "Connection lost. Retry the same request to check its result.");
};

export default function OptionTrading() {
  const [searchParams] = useSearchParams();
  const [underlying, setUnderlying] = useState(() => searchParams.get("underlying") === "BANKNIFTY" ? "BANKNIFTY" : "NIFTY");
  const ticketRef = useRef(null);
  const sellRef = useRef(null);
  const appliedLink = useRef("");
  const { expiry, setExpiry, availableExpiries, error: expiryError } = useExpiryStatus(underlying);
  const [chain, setChain] = useState(null);
  const [selected, setSelected] = useState(null);
  const [lots, setLots] = useState("1");
  const [stop, setStop] = useState("");
  const [target, setTarget] = useState("");
  const [preview, setPreview] = useState(null);
  const [mode, setMode] = useState(null);
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmLive, setConfirmLive] = useState(false);
  const [retryOrder, setRetryOrder] = useState(readPendingOrder);
  const [stateReady, setStateReady] = useState(false);
  const [closing, setClosing] = useState(null);
  const inFlight = useRef(false);
  const ticketVersion = useRef(0);

  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([
      endpoints.executionMode(), endpoints.positions(), endpoints.optionOrders(),
    ]);
    if (results[0].status === "fulfilled") setMode(results[0].value.data.mode);
    if (results[1].status === "fulfilled") setPositions((results[1].value.data.results ?? []).filter((p) => p.option_contract && p.is_open));
    if (results[2].status === "fulfilled") setOrders(results[2].value.data.results ?? []);
    const failed = results.some((result) => result.status === "rejected");
    setStateReady(!failed);
    if (failed) setError("Could not refresh trading state. Refresh before placing an order.");
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    let active = true;
    ticketVersion.current += 1;
    setSelected(null);
    setPreview(null);
    setChain(null);
    if (!expiry) return () => { active = false; };
    const load = () => endpoints.optionChain(underlying, expiry).then((res) => {
      if (active) setChain(res.data);
    }).catch(() => { if (active) setError("Could not load the option chain."); });
    load();
    const timer = setInterval(load, 10000);
    return () => { active = false; clearInterval(timer); };
  }, [underlying, expiry]);

  useEffect(() => {
    ticketVersion.current += 1;
    setPreview(null);
    setConfirmLive(false);
  }, [mode]);

  const unresolved = orders.some((order) => ["processing", "unknown"].includes(order.status));
  const locked = busy || Boolean(retryOrder);
  const edit = (setter, value) => {
    ticketVersion.current += 1;
    setter(value);
    setPreview(null);
    setConfirmLive(false);
    setMessage("");
  };
  const choose = (strike, side) => {
    if (locked) return;
    const row = chain?.rows.find((item) => item.strike === strike);
    const leg = row?.[side === "CE" ? "call" : "put"];
    if (!leg) return;
    edit(setSelected, { contract_id: leg.contract_id, strike, side });
    setStop("");
    setTarget("");
  };
  useEffect(() => {
    const link = searchParams.toString();
    if (!link || appliedLink.current === link || locked || !chain) return;
    const requestedExpiry = searchParams.get("expiry");
    if (requestedExpiry && requestedExpiry !== expiry) {
      if (availableExpiries.includes(requestedExpiry)) setExpiry(requestedExpiry);
      else { setError("The linked expiry is unavailable. Select an available contract."); appliedLink.current = link; }
      return;
    }
    const strike = Number(searchParams.get("strike"));
    const side = searchParams.get("side");
    if (!["CE", "PE"].includes(side)) return;
    const row = chain.rows.find((item) => Number(item.strike) === strike);
    const leg = row?.[side === "CE" ? "call" : "put"];
    if (!leg) { setError("The linked contract is unavailable in this chain."); appliedLink.current = link; return; }
    if (!stateReady) return;
    appliedLink.current = link;
    choose(row.strike, side);
    if (searchParams.get("action") === "sell") {
      const position = positions.find((p) => Number(p.option_contract) === Number(leg.contract_id));
      if (position) { setClosing(position); setConfirmLive(false); }
      else setError("No open position in this contract to sell. Choose a contract you hold.");
    }
  }, [searchParams, chain, expiry, availableExpiries, locked, stateReady, positions, setExpiry]);

  useEffect(() => { if (closing) sellRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }); }, [closing]);
  const heldPositions = selected ? positions.filter((p) => Number(p.option_contract) === Number(selected.contract_id)) : [];
  const showBuyTicket = () => { ticketRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }); ticketRef.current?.querySelector("input")?.focus({ preventScroll: true }); };
  const showSellTicket = () => {
    // Prefer the selected execution mode when the same contract is held in both modes.
    const position = heldPositions.find((p) => p.execution_mode === mode) ?? heldPositions[0];
    if (position) { setClosing(position); setConfirmLive(false); setPreview(null); }
  };
  const ticket = () => ({
    contract_id: selected.contract_id, lots: Number(lots),
    stop_loss: stop, target_price: target, execution_mode: mode,
  });

  const getPreview = async (event) => {
    event.preventDefault();
    if (inFlight.current || !selected || !mode) return;
    inFlight.current = true;
    const version = ticketVersion.current;
    setBusy(true);
    setError("");
    setPreview(null);
    try {
      const res = await endpoints.previewOptionOrder(ticket());
      if (version === ticketVersion.current) setPreview(res.data);
    } catch (err) { setError(errorText(err)); }
    finally { setBusy(false); inFlight.current = false; }
  };

  const submit = async (payload) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    setRetryOrder(payload);
    rememberPendingOrder(payload);
    try {
      const res = await endpoints.placeOptionOrder(payload);
      if (["processing", "unknown"].includes(res.data.status)) {
        setMessage(res.data.message || "Request is processing. Check order history.");
      } else {
        setRetryOrder(null);
        rememberPendingOrder(null);
        setMessage(res.data.message);
        setPreview(null);
        setClosing(null);
        setConfirmLive(false);
      }
    } catch (err) {
      if (err.response && err.response.status < 500) {
        setRetryOrder(null);
        rememberPendingOrder(null);
      }
      setError(errorText(err));
    } finally {
      await refresh();
      setBusy(false);
      inFlight.current = false;
    }
  };

  const buy = () => submit({
    ...ticket(), request_id: newOrderRequestId(), action: "buy", confirm_live: confirmLive,
  });
  const sell = () => submit({
    request_id: newOrderRequestId(), action: "sell", position_id: closing.id,
    execution_mode: closing.execution_mode, confirm_live: confirmLive,
  });

  const positionColumns = [
    { key: "symbol", label: "Contract" },
    { key: "execution_mode", label: "Mode" },
    { key: "qty", label: "Quantity" },
    { key: "entry_price", label: "Entry", render: (p) => money(p.entry_price) },
    { key: "stop_loss", label: "Stop", render: (p) => money(p.stop_loss) },
    { key: "target_price", label: "Target", render: (p) => money(p.target_price) },
    { key: "unrealized_pnl", label: "Net P&L", render: (p) => money(p.unrealized_pnl) },
    { key: "sell", label: "", render: (p) => (
      <button className="btn btn-danger" disabled={locked || unresolved || !stateReady} onClick={() => {
        setClosing(p); setConfirmLive(false); setPreview(null);
      }}>Sell to close</button>
    ) },
  ];
  const orderColumns = [
    { key: "created_at", label: "Time", render: (o) => new Date(o.created_at).toLocaleString() },
    { key: "action", label: "Action" },
    { key: "execution_mode", label: "Mode" },
    { key: "status", label: "Status" },
    { key: "message", label: "Result" },
    { key: "request_id", label: "Request ID" },
  ];

  return (
    <div className="option-trading">
      <div className="panel">
        <div className="option-trading-heading">
          <div><h2>Option Trading</h2><p>Buy a call or put. Sell an existing position to close.</p></div>
          <span className={mode === "live" ? "badge badge-red" : "badge badge-accent"}>
            {mode ? mode.toUpperCase() : "LOADING MODE"}
          </span>
        </div>
        <p>Orders follow the trading mode in <Link to="/settings">Settings</Link>. Stops and targets are monitored by the running execution worker.</p>
        {(error || expiryError) && <p role="alert" className="num-negative">{error || expiryError}</p>}
        {message && <p role="status">{message}</p>}
        {unresolved && <p role="status">An order is awaiting reconciliation. Check its result before submitting another order.</p>}
        {retryOrder && <p>Pending {retryOrder.action} request: {retryOrder.request_id}</p>}
        {retryOrder && <button className="btn" disabled={busy} onClick={() => submit(retryOrder)}>Check / retry same request</button>}
      </div>

      {selected && <OptionContractChartModal key={`${selected.contract_id}:${expiry}`} embedded
        contract={{ strike: selected.strike, optionType: selected.side }} underlying={underlying} expiry={expiry}
        chainRows={chain?.rows ?? []} onBuy={showBuyTicket} onSell={showSellTicket}
        canSell={heldPositions.length > 0} tradingDisabled={locked || unresolved || !stateReady} />}
      <div className="option-trading-grid">
        <div className="panel">
          <h3>Select a contract</h3>
          <div className="option-ticket-fields">
            <label>Underlying<select disabled={locked} value={underlying} onChange={(e) => edit(setUnderlying, e.target.value)}>
              <option>NIFTY</option><option>BANKNIFTY</option>
            </select></label>
            <label>Expiry<select disabled={locked} value={expiry} onChange={(e) => edit(setExpiry, e.target.value)}>
              {availableExpiries.map((item) => <option key={item}>{item}</option>)}
            </select></label>
          </div>
          <p>Click a call or put premium in the chain to select it.</p>
          <OptionChainTable rows={chain?.rows ?? []} spot={chain?.spot}
            selectedContractStrike={selected?.strike} selectedContractSide={selected?.side} onCellClick={choose} />
          {chain && !chain.rows.length && <p>No quotes available for this expiry.</p>}
        </div>
        <div className="panel" ref={ticketRef}>
          <h3>Buy option</h3>
          <p>{selected ? `${underlying} ${selected.strike} ${selected.side} · ${chain?.expiry ?? expiry}` : "Select a call or put from the chain."}</p>
          <form onSubmit={getPreview}>
            <fieldset disabled={locked || !selected || !mode || unresolved || !stateReady} className="option-ticket-fields">
              <label>Lots<input type="number" min="1" step="1" required value={lots} onChange={(e) => edit(setLots, e.target.value)} /></label>
              <label>Stop-loss premium<input type="number" min="0.0001" step="0.0001" required value={stop} onChange={(e) => edit(setStop, e.target.value)} /></label>
              <label>Target premium<input type="number" min="0.0001" step="0.0001" required value={target} onChange={(e) => edit(setTarget, e.target.value)} /></label>
              <button className="btn" type="submit">Preview order</button>
            </fieldset>
          </form>
          {preview && <>
            <dl className="option-order-preview">
              <dt>Contract</dt><dd>{preview.tradingsymbol}</dd>
              <dt>Quantity</dt><dd>{preview.quantity} ({preview.lot_size} per lot)</dd>
              <dt>Estimated premium</dt><dd>₹{money(preview.premium)}</dd>
              <dt>Premium outlay</dt><dd>₹{money(preview.premium_outlay)}</dd>
              <dt>Risk to stop</dt><dd>₹{money(preview.stop_risk)}</dd>
              <dt>Profit at target</dt><dd>₹{money(preview.target_profit)}</dd>
            </dl>
            <p>Estimates exclude fees and slippage. Market fills can differ; a stop does not guarantee an exit price. Risk limits may reduce the filled paper quantity.</p>
            {mode === "live" && <label className="option-live-confirm"><input type="checkbox" checked={confirmLive} disabled={locked} onChange={(e) => setConfirmLive(e.target.checked)} /> I confirm this buy uses real money.</label>}
            <button className="btn btn-primary" disabled={locked || unresolved || !stateReady || (mode === "live" && !confirmLive)} onClick={buy}>
              {busy ? "Submitting…" : `Buy ${selected.side} · ${mode.toUpperCase()}`}
            </button>
          </>}
        </div>
      </div>
      {closing && <div ref={sellRef} className="panel" role="region" aria-label="Confirm sell to close">
        <h3>Sell to close {closing.symbol}</h3>
        <p>Sell all {closing.qty} units in {closing.execution_mode.toUpperCase()} mode at the available market price.</p>
        {closing.execution_mode === "live" && <label className="option-live-confirm"><input type="checkbox" checked={confirmLive} disabled={locked} onChange={(e) => setConfirmLive(e.target.checked)} /> I confirm this sell is a real broker order.</label>}
        <button className="btn btn-danger" disabled={locked || unresolved || !stateReady || (closing.execution_mode === "live" && !confirmLive)} onClick={sell}>Confirm sell to close</button>
        <button className="btn" disabled={locked} onClick={() => setClosing(null)}>Cancel</button>
      </div>}
      <div className="panel"><h3>Open option positions</h3>
        <DataTable columns={positionColumns} rows={positions} getRowKey={(p) => p.id} emptyTitle="No open option positions" />
      </div>
      <div className="panel"><h3>Your order history</h3>
        <DataTable columns={orderColumns} rows={orders} getRowKey={(o) => o.request_id} emptyTitle="No manual option orders yet" />
      </div>
    </div>
  );
}

