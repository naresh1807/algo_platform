import { useEffect, useState } from "react";

import { api, endpoints } from "../services/api.js";

// apps.learning.strategy_methods.METHOD_FUNCS's three comparison
// methods, run continuously and isolated from real trading (see
// apps/learning/models.py's HypotheticalTrade docstring) -- their
// win/loss record is written to ModelRegistry as
// model_name=f"strategy_comparison_{method}" by
// apps.learning.tasks.evaluate_strategy_methods (daily). Reusing the
// SAME mlModels fetch (endpoints.modelRegistry()) the Win-Probability
// panel below already loads -- no new endpoint needed, just a
// different client-side filter on model_name.
const STRATEGY_METHODS = [
  { name: "trend_following", label: "Trend Following" },
  { name: "mean_reversion", label: "Mean Reversion" },
  { name: "breakout", label: "Breakout" },
];

/**
 * The approve/reject action on a DailyReviewNote is the one human
 * governance step the entire learning loop hinges on (manual section
 * 14 & 15) -- nothing in suggested_changes is ever applied to a
 * StrategyVersion until approved_flag flips to true here. This page is
 * intentionally the only place in the whole UI that writes anything
 * (a PATCH), everywhere else is read-only, to keep that boundary clear.
 */
export default function Learning() {
  const [reviews, setReviews] = useState([]);
  const [driftEvents, setDriftEvents] = useState([]);
  const [strategyVersions, setStrategyVersions] = useState([]);
  const [mlModels, setMlModels] = useState([]);
  const [tradeReviews, setTradeReviews] = useState([]);

  const loadReviews = () => endpoints.dailyReviews().then((res) => setReviews(res.data.results ?? []));

  useEffect(() => {
    loadReviews();
    endpoints.driftEvents().then((res) => setDriftEvents(res.data.results ?? []));
    endpoints.strategyVersions().then((res) => setStrategyVersions(res.data.results ?? []));
    endpoints.modelRegistry().then((res) => setMlModels(res.data.results ?? []));
    endpoints.tradeReviews().then((res) => setTradeReviews(res.data.results ?? []));
  }, []);

  const approve = async (review) => {
    await api.patch(`/learning/daily-reviews/${review.id}/`, { approved_flag: true });
    loadReviews();
  };

  return (
    <>
      <div className="panel">
        <h3>Strategy Versions</h3>
        {strategyVersions.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No strategy versions created yet.</p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            {strategyVersions.map((v) => (
              <li key={v.id}>
                {v.version_name} {v.active_flag && <strong>(active)</strong>}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="panel">
        <h3>Daily Review Notes</h3>
        {reviews.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No reviews yet -- generated automatically after market close.</p>
        ) : (
          reviews.map((r) => (
            <div key={r.id} style={{ borderTop: "1px solid var(--border)", padding: "8px 0" }}>
              <strong>{r.review_date}</strong>
              <p style={{ fontSize: 13 }}>{r.summary}</p>
              {r.approved_flag ? (
                <span style={{ color: "var(--green)", fontSize: 12 }}>✅ Approved</span>
              ) : (
                <button
                  onClick={() => approve(r)}
                  style={{
                    padding: "4px 10px",
                    borderRadius: 6,
                    border: "1px solid var(--accent)",
                    background: "transparent",
                    color: "var(--accent)",
                    cursor: "pointer",
                  }}
                >
                  Approve suggested changes
                </button>
              )}
            </div>
          ))
        )}
      </div>

      <div className="panel">
        <h3>Win-Probability ML Model</h3>
        {/* apps.learning.ml_train trains this weekly, once enough closed
            trades exist (MIN_TRAINING_SAMPLES) -- until then this list is
            empty, which is an expected cold-start state, not an error. */}
        {mlModels.filter((m) => m.model_name === "win_probability").length === 0 ? (
          <p style={{ color: "var(--muted)" }}>
            No model trained yet -- needs enough closed paper trades first.
          </p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            {mlModels
              .filter((m) => m.model_name === "win_probability")
              .map((m) => (
                <li key={m.id}>
                  v{m.model_version} {m.active_flag && <strong>(active)</strong>} — trained on{" "}
                  {m.metrics_json?.sample_count ?? "?"} trades, accuracy{" "}
                  {m.metrics_json?.accuracy != null ? `${(m.metrics_json.accuracy * 100).toFixed(0)}%` : "n/a"}
                  {m.metrics_json?.eval_type === "train_only_no_holdout" && " (train-set only, no holdout yet)"}
                </li>
              ))}
          </ul>
        )}
      </div>

      <div className="panel">
        <h3>Strategy Comparison</h3>
        <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 0 }}>
          Three genuinely different paper-trading methods run continuously and isolated from
          real trading (never touch real positions/equity) -- their win/loss record is evaluated
          daily and the current best performer is marked below. See apps.learning.strategy_methods.
        </p>
        <div className="card-grid">
          {STRATEGY_METHODS.map(({ name, label }) => {
            const registryRow = mlModels
              .filter((m) => m.model_name === `strategy_comparison_${name}`)
              .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];
            const metrics = registryRow?.metrics_json;
            const isActive = registryRow?.active_flag;
            return (
              <div key={name} className={`card ${isActive ? "card-accent-green" : "card-accent-muted"}`}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontWeight: 600 }}>{label}</span>
                  {isActive && <span className="badge badge-green">Best</span>}
                </div>
                {metrics ? (
                  <div style={{ marginTop: 8, fontSize: 12, color: "var(--muted)" }}>
                    <div style={{ fontSize: 20, fontWeight: 700, color: "var(--text)" }}>
                      {(metrics.win_rate * 100).toFixed(0)}% win rate
                    </div>
                    <div style={{ marginTop: 4 }}>{metrics.trade_count} closed trade(s)</div>
                    <div>
                      Avg R: {metrics.avg_r_multiple != null ? `${metrics.avg_r_multiple.toFixed(2)}R` : "n/a"}
                    </div>
                    <div style={{ color: metrics.total_pnl >= 0 ? "var(--green)" : "var(--red)" }}>
                      Total P&amp;L: {metrics.total_pnl >= 0 ? "+" : ""}{metrics.total_pnl.toFixed(2)}
                    </div>
                  </div>
                ) : (
                  <p style={{ color: "var(--muted)", fontSize: 12, marginTop: 8 }}>
                    Not enough closed trades yet -- evaluated daily after market close.
                  </p>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <div className="panel">
        <h3>Drift Events</h3>
        {driftEvents.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No drift detected.</p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            {driftEvents.map((d) => (
              <li key={d.id}>
                {d.drift_type} / {d.metric_name} — {d.severity}
                {d.action_taken && ` (action: ${d.action_taken})`}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="panel">
        <h3>Trade Reviews (Experience Memory)</h3>
        {/* manual 13.8 Experience Memory / 11.10 Reward Engine / 13.14
            Mistake Analysis -- one TradeReview row per closed position,
            created by apps.learning.signals as each trade closes. */}
        {tradeReviews.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No closed trades reviewed yet.</p>
        ) : (
          <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                <th>Symbol</th>
                <th>Reward</th>
                <th>R-multiple</th>
                <th>Confidence</th>
                <th>Mistake tags</th>
              </tr>
            </thead>
            <tbody>
              {tradeReviews.slice(0, 15).map((r) => (
                <tr key={r.id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td>{r.symbol}</td>
                  <td style={{ color: r.reward_score >= 0 ? "var(--green)" : "var(--red)" }}>
                    {r.reward_score != null ? (r.reward_score > 0 ? `+${r.reward_score}` : r.reward_score) : "n/a"}
                  </td>
                  <td>{r.r_multiple != null ? `${r.r_multiple.toFixed(2)}R` : "n/a"}</td>
                  <td>{r.confidence_band}</td>
                  <td>{r.mistake_tags?.length ? r.mistake_tags.join(", ") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
