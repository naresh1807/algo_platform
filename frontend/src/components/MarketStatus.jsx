import { useEffect, useState } from "react";

import { marketOpenStatus } from "../utils/marketHours.js";
import StatusBadge from "./StatusBadge.jsx";

export default function MarketStatus() {
  const [status, setStatus] = useState(marketOpenStatus);

  useEffect(() => {
    const id = setInterval(() => setStatus(marketOpenStatus()), 30000);
    return () => clearInterval(id);
  }, []);

  return (
    <span title={status.detail}>
      <StatusBadge tone={status.open ? "ok" : "muted"}>
        {status.open ? "Market Open" : "Market Closed"}
      </StatusBadge>
    </span>
  );
}
