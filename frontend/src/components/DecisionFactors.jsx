// Mirrors the default apps.risk.engine.check_option_contract_liquidity
// threshold (settings.RISK_HARD_LIMITS["MAX_OPTION_BID_ASK_SPREAD_PCT"])
// -- shown here purely as an informational reference for the spread
// row below, not re-implementing the actual risk gate (the backend
// already ran it before this signal was ever created/approved).
const REFERENCE_MAX_SPREAD_PCT = 5.0;

function Row({ label, ok, detail, unknown }) {
  return (
    <div
      style={{
        display: "flex", gap: 10, alignItems: "flex-start",
        padding: "6px 0", borderTop: "1px solid var(--border)",
      }}
    >
      <span className={`status-dot ${unknown ? "warn" : ok ? "ok" : "bad"}`} style={{ marginTop: 5 }} />
      <div style={{ flex: 1 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
          <span style={{ fontWeight: 600 }}>{label}</span>
          <span style={{ color: unknown ? "var(--muted)" : ok ? "var(--green)" : "var(--red)", fontSize: 12 }}>
            {unknown ? "No data" : ok ? "✓" : "✗"}
          </span>
        </div>
        {detail && <div style={{ fontSize: 12, color: "var(--muted)" }}>{detail}</div>}
      </div>
    </div>
  );
}

/**
 * "AI Decision Factors" checklist -- Trend/Momentum/Volume/OI/IV/
 * Liquidity/Spread/Delta/Gamma/Theta/Expected-Move/Risk-Reward, styled
 * like OptionsAnalytics.jsx's existing options-signals checklist rows
 * (status-dot + label + detail) for visual consistency with the rest
 * of the app.
 *
 * Every row is derived from fields already on the TradingSignal object
 * (apps.signals.models.TradingSignal's ind_* snapshot, options_score)
 * or the matching option-chain row (greeks/OI/volume/bid/ask, from
 * apps.options.views.OptionChainView) -- this is an EXPLANATION of a
 * decision the backend already made (apps.risk.engine.check_pre_trade /
 * check_option_contract_liquidity), not a second, independent check
 * re-run in the browser. When a factor's underlying data isn't present
 * on the signal (e.g. a pre-option-contract-era signal, or a chain row
 * that hasn't loaded), the row says "No data" rather than guessing.
 */
export default function DecisionFactors({ signal, chainRow }) {
  if (!signal) return null;

  const isBullish = signal.option_side === "CE" || signal.signal_type === "buy";
  const leg = signal.option_side === "PE" ? chainRow?.put : chainRow?.call;

  const ema9 = signal.ind_ema9_slope_pct;
  const ema21 = signal.ind_ema21_slope_pct;
  const trendKnown = ema9 != null && ema21 != null;
  const trendOk = trendKnown && (isBullish ? ema9 > 0 && ema21 > 0 : ema9 < 0 && ema21 < 0);

  const macd = signal.ind_macd_hist_pct;
  const momentumKnown = macd != null;
  const momentumOk = momentumKnown && (isBullish ? macd > 0 : macd < 0);

  const relVol = signal.ind_relative_volume;
  const volumeKnown = relVol != null;
  const volumeOk = volumeKnown && relVol >= 1;

  const oiKnown = leg?.open_interest != null;
  const oiOk = oiKnown && leg.open_interest > 0;

  const optionsScoreKnown = signal.options_score != null;
  const optionsScoreOk = optionsScoreKnown && signal.options_score >= 0.6;

  const ivKnown = leg?.iv != null;

  const liquidityKnown = leg?.open_interest != null && leg?.volume != null;
  const liquidityOk = liquidityKnown && leg.open_interest > 0;

  const spreadKnown = leg?.bid != null && leg?.ask != null && leg.bid > 0;
  const spreadPct = spreadKnown ? ((leg.ask - leg.bid) / ((leg.ask + leg.bid) / 2)) * 100 : null;
  const spreadOk = spreadKnown && spreadPct <= REFERENCE_MAX_SPREAD_PCT;

  const deltaKnown = leg?.greeks?.delta != null;
  const deltaOk = deltaKnown && Math.abs(leg.greeks.delta) >= 0.3 && Math.abs(leg.greeks.delta) <= 0.7;

  const gammaKnown = leg?.greeks?.gamma != null;
  const thetaKnown = leg?.greeks?.theta != null; // no independent pass/fail threshold -- informational only

  const moveKnown = signal.entry_price != null && signal.stop_loss != null;
  const moveDistance = moveKnown ? Math.abs(Number(signal.entry_price) - Number(signal.stop_loss)) : null;

  const rrKnown = signal.entry_price != null && signal.stop_loss != null && signal.target_1 != null;
  const rr = rrKnown
    ? Math.abs(Number(signal.target_1) - Number(signal.entry_price)) / Math.abs(Number(signal.entry_price) - Number(signal.stop_loss))
    : null;
  const rrOk = rr != null && rr >= 1.2;

  return (
    <div className="panel">
      <h3>AI Decision Factors</h3>
      <Row label="Trend" unknown={!trendKnown} ok={trendOk}
        detail={trendKnown ? `EMA9 slope ${(ema9 * 100).toFixed(2)}% / EMA21 slope ${(ema21 * 100).toFixed(2)}%` : null} />
      <Row label="Momentum" unknown={!momentumKnown} ok={momentumOk}
        detail={momentumKnown ? `MACD histogram ${(macd * 100).toFixed(3)}% of price` : null} />
      <Row label="Volume" unknown={!volumeKnown} ok={volumeOk}
        detail={volumeKnown ? `${relVol.toFixed(2)}x the 20-period average` : null} />
      <Row label="Open Interest" unknown={!oiKnown} ok={oiOk}
        detail={oiKnown ? `${leg.open_interest.toLocaleString()} contracts` : null} />
      <Row label="OI Structure" unknown={!optionsScoreKnown} ok={optionsScoreOk}
        detail={optionsScoreKnown ? `Options-chain confluence score ${signal.options_score.toFixed(2)}` : null} />
      <Row label="IV" unknown={!ivKnown} ok={ivKnown}
        detail={ivKnown ? `${leg.iv}%` : null} />
      <Row label="Liquidity" unknown={!liquidityKnown} ok={liquidityOk}
        detail={liquidityKnown ? `OI ${leg.open_interest.toLocaleString()}, volume ${leg.volume.toLocaleString()}` : null} />
      <Row label="Bid/Ask Spread" unknown={!spreadKnown} ok={spreadOk}
        detail={spreadKnown ? `${spreadPct.toFixed(1)}% (reference limit ${REFERENCE_MAX_SPREAD_PCT}%)` : null} />
      <Row label="Delta" unknown={!deltaKnown} ok={deltaOk}
        detail={deltaKnown ? `${leg.greeks.delta.toFixed(2)} (probability proxy)` : null} />
      <Row label="Gamma" unknown={!gammaKnown} ok={gammaKnown}
        detail={gammaKnown ? leg.greeks.gamma.toFixed(4) : null} />
      <Row label="Theta" unknown={!thetaKnown} ok={thetaKnown}
        detail={thetaKnown ? `${leg.greeks.theta.toFixed(2)}/day` : null} />
      <Row label="Move to Stop" unknown={!moveKnown} ok={moveKnown}
        detail={moveKnown ? `₹${moveDistance.toFixed(2)} (ATR-based stop distance)` : null} />
      <Row label="Risk/Reward" unknown={!rrKnown} ok={rrOk}
        detail={rrKnown ? `1 : ${rr.toFixed(2)}` : null} />
    </div>
  );
}
