import { Bot } from "lucide-react";
import { Link } from "react-router-dom";

import ConfidenceIndicator from "./ConfidenceIndicator.jsx";
import StatusBadge from "./StatusBadge.jsx";

const REJECTION_STAGE_LABELS = {
  direction: "Underlying Direction",
  success_rate: "Historical Success Rate",
  sentiment: "Sentiment Veto",
  options_confluence: "Options-Chain Confluence",
  expiry: "Expiry Selection",
  strike: "Strike Selection",
  liquidity: "Liquidity Validation",
  risk: "Risk Validation",
  ml_confidence: "ML Confidence",
  lot_size: "Lot-Size Rounding",
};

function fmtPrice(value) {
  if (value == null) return "—";
  const n = Number(value);
  return `₹${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function riskReward(signal) {
  const entry = Number(signal.entry_price);
  const stop = Number(signal.stop_loss);
  const target = signal.target_1 != null ? Number(signal.target_1) : null;
  if (target == null || !Number.isFinite(entry) || !Number.isFinite(stop) || !Number.isFinite(target)) return null;
  const risk = Math.abs(entry - stop);
  const reward = Math.abs(target - entry);
  if (risk === 0) return null;
  return reward / risk;
}

/**
 * The main "JARVIS AI SIGNAL" card -- takes an apps.signals.models.
 * TradingSignal object (as returned by TradingSignalSerializer, or the
 * leaner apps.signals.signals.py WebSocket broadcast payload merged
 * with it) and renders it as an actionable trade card: WHAT to trade,
 * WHY, AT WHAT PRICE, WHERE TO EXIT, HOW MUCH RISK.
 *
 * Modeled on components/SignalAlertPopup.jsx's existing field set
 * (entry_price/stop_loss/target_1/target_2/total_score/regime/
 * option_side/strike_price -- that popup already proves these fields
 * are populated end-to-end) but expanded into a full card: contract
 * identity, confidence %, option LTP, risk:reward, and the reason text.
 *
 * Deliberately does NOT invent any value not already on the signal
 * object -- e.g. no fabricated "entry range", since the backend signal
 * only ever carries a single entry_price (see apps.signals.models.
 * TradingSignal), matching this platform's "never invent price/strike/
 * expiry/OI/volume" safety rule.
 */
export default function SignalCard({ signal }) {
  if (!signal) {
    return (
      <div className="panel">
        <h3 style={{ color: "var(--jarvis)", display: "flex", alignItems: "center", gap: 6 }}>
          <Bot size={14} /> JARVIS AI Signal
        </h3>
        <p style={{ color: "var(--muted)", fontSize: 13 }}>No signal generated yet for this underlying.</p>
      </div>
    );
  }

  const hasContract = signal.option_contract_id != null || signal.option_contract != null;
  const contract = signal.option_contract_detail || null;
  const isApproved = signal.status === "approved" || signal.status === "executed";
  const isNoTrade = signal.signal_type === "no_trade" || signal.status === "rejected";

  const confidencePct = signal.ml_win_probability != null
    ? signal.ml_win_probability * 100
    : signal.total_score != null
      ? signal.total_score * 100
      : null;

  const rr = riskReward(signal);

  const contractLabel = hasContract && signal.strike_price != null && signal.option_side
    ? `${signal.signal_type?.toUpperCase() || "BUY"} ${Number(signal.strike_price)} ${signal.option_side}`
    : `${signal.signal_type?.toUpperCase() || "NO TRADE"} ${signal.symbol}`;

  // The backend never fabricates option data -- a NO_TRADE whose reason
  // text starts with "OPTION DATA UNAVAILABLE" or "TRADE BLOCKED" (see
  // apps.risk.engine.check_option_contract_liquidity and apps.options.
  // signals_engine.select_expiry) means exactly that literally, so
  // surface it verbatim rather than a generic "no signal" message.
  const reasonUpper = (signal.reason || "").toUpperCase();
  const isDataUnavailable = reasonUpper.includes("OPTION DATA UNAVAILABLE");
  const isBlocked = isNoTrade && (isDataUnavailable || reasonUpper.includes("TRADE BLOCKED") || signal.rejection_stage);

  return (
    <div className="panel" style={{ borderTop: "2px solid var(--jarvis)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
        <h3 style={{ margin: 0, color: "var(--jarvis)", display: "flex", alignItems: "center", gap: 6 }}>
          <Bot size={14} /> JARVIS AI Signal
        </h3>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {signal.risk_score != null && (
            <StatusBadge tone={signal.risk_score >= 0.8 ? "ok" : signal.risk_score > 0 ? "warn" : "bad"}>
              Risk check {signal.risk_score >= 0.8 ? "passed" : signal.risk_score > 0 ? "partial" : "failed"}
            </StatusBadge>
          )}
          {confidencePct != null && <ConfidenceIndicator value={confidencePct} />}
        </div>
      </div>

      <div style={{ marginTop: 6 }}>
        <span
          style={{
            fontSize: 22, fontWeight: 700,
            color: isApproved ? "var(--green)" : isBlocked ? "var(--red)" : "var(--muted)",
          }}
        >
          {isDataUnavailable ? "OPTION DATA UNAVAILABLE" : isBlocked ? "TRADE BLOCKED" : contractLabel}
        </span>
      </div>

      {isBlocked && signal.rejection_stage && (
        <p style={{ fontSize: 12, color: "var(--muted)", margin: "4px 0 0" }}>
          Died at: {REJECTION_STAGE_LABELS[signal.rejection_stage] || signal.rejection_stage}
        </p>
      )}

      {hasContract && contract && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 8, marginTop: 12, fontSize: 13 }}>
          <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Expiry</div>{contract.expiry}</div>
          <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Strike</div>{Number(contract.strike)}</div>
          <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Option</div>{contract.option_type}</div>
          <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Option LTP</div>{fmtPrice(signal.entry_price)}</div>
        </div>
      )}

      {isApproved && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 8, marginTop: 12, fontSize: 13 }}>
          <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Entry</div>{fmtPrice(signal.entry_price)}</div>
          <div><div style={{ color: "var(--red)", fontSize: 11 }}>Stop Loss</div>{fmtPrice(signal.stop_loss)}</div>
          <div><div style={{ color: "var(--green)", fontSize: 11 }}>Target 1</div>{fmtPrice(signal.target_1)}</div>
          {signal.target_2 != null && (
            <div><div style={{ color: "var(--green)", fontSize: 11 }}>Target 2</div>{fmtPrice(signal.target_2)}</div>
          )}
          {rr != null && (
            <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Risk/Reward</div>1 : {rr.toFixed(2)}</div>
          )}
          {signal.position_size != null && (
            <div><div style={{ color: "var(--muted)", fontSize: 11 }}>Quantity</div>{signal.position_size}</div>
          )}
        </div>
      )}

      {isApproved && signal.stop_loss != null && (
        <p style={{ fontSize: 11.5, color: "var(--amber)", marginTop: 10, marginBottom: 0 }}>
          Invalidates if price hits the stop-loss level above before target.
        </p>
      )}

      {signal.reason && (
        <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 12, lineHeight: 1.5 }}>{signal.reason}</p>
      )}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 10 }}>
        {signal.created_at && (
          <span style={{ fontSize: 11, color: "var(--muted)" }}>
            {new Date(signal.created_at).toLocaleString()}
          </span>
        )}
        {signal.symbol && (
          <Link to="/jarvis-signals" style={{ fontSize: 11.5, color: "var(--jarvis)", fontWeight: 600, textDecoration: "none" }}>
            View chart →
          </Link>
        )}
      </div>
    </div>
  );
}
