import { useEffect, useState } from "react";

import DataTable from "../components/DataTable.jsx";
import { endpoints } from "../services/api.js";

export default function Positions() {
  const [positions, setPositions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = () => {
    setLoading(true);
    setError(null);
    endpoints.positions()
      .then((res) => setPositions(res.data.results ?? []))
      .catch(() => setError("Could not load positions."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const columns = [
    { key: "symbol", label: "Symbol" },
    { key: "side", label: "Side", render: (p) => p.side.toUpperCase() },
    { key: "qty", label: "Qty", align: "right" },
    { key: "entry_price", label: "Entry", align: "right", render: (p) => p.entry_price },
    {
      key: "stop_loss", label: "Stop-Loss", align: "right",
      render: (p) => (
        <>
          {p.stop_loss}
          {p.trailing_stop_distance != null && (
            <span
              title={`Trailing by ${p.trailing_stop_distance} -- peak so far ${p.peak_price}`}
              style={{ marginLeft: 6, fontSize: 11, color: "var(--accent)" }}
            >
              ▲ trailing
            </span>
          )}
        </>
      ),
    },
    { key: "target_price", label: "Target", align: "right", render: (p) => p.target_price ?? "—" },
    {
      key: "unrealized_pnl", label: "Unrealized P&L", align: "right",
      render: (p) => <span className={Number(p.unrealized_pnl) >= 0 ? "num-positive" : "num-negative"}>{p.unrealized_pnl}</span>,
    },
    { key: "opened_at", label: "Opened", render: (p) => new Date(p.opened_at).toLocaleString() },
  ];

  return (
    <div className="panel">
      <h3>Open Positions</h3>
      <DataTable
        columns={columns}
        rows={positions}
        getRowKey={(p) => p.id}
        searchKeys={["symbol", "side"]}
        searchPlaceholder="Search positions…"
        loading={loading}
        error={error}
        onRetry={load}
        emptyTitle="No open positions"
        emptyDetail="Approved signals open real (or paper) positions automatically."
      />
    </div>
  );
}
