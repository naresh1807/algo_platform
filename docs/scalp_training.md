# Drishti

The trading model display name is **Drishti**. Internal registry identifiers are preserved for model loading and version history.

# Scalping candidate training: 6 October 2026

Run: `backend/.venv/Scripts/python.exe backend/manage.py train_scalp_candidate --output-dir backend/ml_artifacts/<unique-run-directory>`.
The command trains an inactive `scalp_selection_candidate`, without changing the active advisory model or execution settings.

The run used 359 recorded closed scalps. 56 lacked entry indicators and were excluded. Relative volume was unavailable in all rows, so that feature was omitted. One overlapping trade was purged. Splits preserve whole trading days: 161 training rows over five days, 76 validation rows over two days, and 65 test rows over two days. A twelve-minute embargo prevents boundary label overlap.

Logistic regression was selected by validation Brier score against gradient boosting. The fixed probability cutoff is 0.60. On the held-out test: AUC 0.6614; Brier 0.2184 vs training-prior baseline 0.2544; 9 selected trades, 6 wins, mean gross R 0.6164. The full test set has 30 wins out of 65 and mean R 0.00746.

Artifacts: `backend/ml_artifacts/scalp_candidate_20261006/scalp_candidate.joblib` and `report.json`. Registry ID 88 is inactive. These outcomes are hypothetical underlying scalps, not option premium fills. Charges, spreads and actual broker fills are unavailable; an illustrative 0.1R cost sensitivity is included in the report. Do not interpret the fitted probabilities or nine selected outcomes as a verified future win rate. Further forward paper evaluation across more market sessions is required before considering execution integration.

Four regression tests cover whole-day separation, purging, insufficient days, and zero qualifying trades. Existing trading models and execution settings were not modified.


## Direct market-data training: 6 October 2026

The model was also trained directly on 120,000 stored one-minute candles: 60,000 each for NIFTY and BANKNIFTY, sourced from `angel_one` and `angel_one_live`. These are real recorded candles, not synthetic prices. The pipeline generated 17,804 simulated long/short training examples with entry indicators known before the next-bar opening fill.

Command: `backend/.venv/Scripts/python.exe backend/manage.py train_scalp_market --output-dir backend/ml_artifacts/scalp_market_20261006`.

Labels simulate a 0.6 ATR stop, 1R target and 12-minute horizon. Stop-first resolution is used when both barriers touch in one bar; adverse gaps fill at the opening price. Discontinuous and overnight windows are excluded. The assumed round-trip cost is 0.1R; this is a sensitivity assumption, not observed brokerage or bid/ask spread.

Whole-day split: 10,760 examples across 97 training days; 3,576 across 32 validation days; 3,468 across 33 test days. Long/short examples for the same window are correlated. Logistic regression had the better validation Brier score. Test AUC: 0.5631; Brier: 0.2475 versus constant-prior baseline 0.2499. At the predeclared 0.60 probability cutoff it selected **zero test trades**. This is weak evidence of ranking ability and no evidence for profitable selected trades at that cutoff. The cutoff was not lowered after inspecting the test result.

Registry ID 89 (`scalp_market_candidate`) remains inactive. Saved artifacts under `backend/ml_artifacts/scalp_market_20261006/20261006110411887564/`: `dataset.csv`, `model.joblib`, `report.json`. Volume is unavailable for these spot indices and excluded from features. This dataset models underlying direction, not executable option-premium returns. It does not update active models, operational trade records, or execution settings.

Nine checks passed for temporal separation, overlap purging, causal entry indicators, stop/target ambiguity, short labels, adverse gaps and timeout exits. The market-data candidate requires further validation before it can be used for trading.


## Strategy-specific learning

Command: `backend/.venv/Scripts/python.exe backend/manage.py train_scalp_market --strategy-setups --output-dir backend/ml_artifacts/scalp_strategies_20261006`.

Historical replay uses the same `scalp_idea_from_indicators` function as the existing runtime generators:

- EMA momentum: EMA9 above EMA21 and a positive, rising MACD histogram; 0.6 ATR stop and 1R target.
- RSI extreme: RSI at or below the configured extreme threshold, outside the high-volatility regime; 0.5 ATR stop and 1.2R target.
- SAR burst: SAR below price, with the configured volume condition when volume is available; stop at SAR and 1.5R target.

Existing rules generate bullish setups only. Spot-index volume is unavailable, so volume confirmation is skipped exactly as in the runtime rules. Stops and targets are anchored to the signal candle; the next-bar open must remain between them. Training samples are spaced by at least the full holding horizon within each strategy/symbol. Different strategies may still overlap, so summed results are not an executable portfolio backtest.

The candidate learns scores from entry indicators plus strategy and symbol identities. Strategy conditions remain explicit rules. Validation chooses between logistic regression and gradient boosting by Brier score. The held-out test and each strategy's test subset report selected outcomes at the fixed 0.60 cutoff. No model is promoted or connected to execution automatically.


Strategy run completed: registry ID 90, inactive `scalp_strategy_candidate`. Artifacts: `backend/ml_artifacts/scalp_strategies_20261006/20261006111447024123/` (`dataset.csv`, `model.joblib`, `report.json`). The candidate learned from 10,565 strategy examples: 6,398 training across 97 days, 2,124 validation across 32 days, and 2,043 test across 33 days. Gradient boosting won validation selection. Held-out AUC was 0.5364 and Brier was 0.2420 versus baseline 0.2427, indicating weak predictive evidence. The fixed 0.60 cutoff selected no test trades.

| Strategy | Test examples | Simulated win rate | Mean net R |
| --- | ---: | ---: | ---: |
| EMA momentum | 723 | 46.9% | -0.241 |
| RSI extreme | 50 | 38.0% | -0.342 |
| SAR burst | 1,270 | 37.7% | -0.197 |

These values include the assumed 0.1R cost per example, not actual option execution costs. None of the three strategies showed positive average simulated return in this held-out period. No strategy or model was promoted based on these results. Six existing runtime-rule regression tests plus ten label/split/causality tests passed (16 total).


## Activation: 6 October 2026

At the user's explicit request, candidate 90 was verified and copied into the existing `scalp_win_probability` advisory inference slot as active registry ID **91**. The previous active advisory version was deactivated; source candidate 90 remains preserved. Activation command: `backend/.venv/Scripts/python.exe backend/manage.py activate_scalp_candidate 90`.

The activation verifies artifact location, feature schema, feature count, class order and finite probability output before changing the registry atomically. The real inference loader successfully scored recorded scalp 394 at 0.3593. This score is not a demonstrated future win rate. Model loading refreshes automatically when the active registry ID changes.

The model scores new hypothetical scalping comparison trades through `predict_scalp_win_probability`. This pathway is advisory: it does not filter entries or place broker orders. Execution mode remained `paper`; risk and kill-switch settings were not modified. Scheduled retraining retains its existing ability to replace the advisory champion if its promotion gate passes. Held-out weaknesses documented above remain unchanged by activation.


## Return-focused training: 6 October 2026

Trained inactive model **92**, `scalp_expected_return_candidate`, to estimate simulated net R rather than binary win probability. Reproducible command:

`backend/.venv/Scripts/python.exe backend/manage.py train_scalp_returns --dataset backend/ml_artifacts/scalp_strategies_20261006/20261006111447024123/dataset.csv --output-dir backend/ml_artifacts/scalp_returns_20261006`

Ridge regression and robust gradient boosting were compared on the same day-separated strategy dataset. The training target includes the assumed 0.1R cost. Training-label influence was bounded using only training-set quantiles; reported outcomes remain unclipped. Validation evaluated the predefined estimated-net-R cutoffs 0, 0.05, 0.1, 0.2 and 0.3. A qualifying cutoff needs at least 50 selected examples across ten days and a positive lower bound for the day-mean bootstrap interval.

**No validation cutoff passed.** Robust gradient boosting was saved as the diagnostic candidate with lower validation prediction error, but its selection threshold is null and selection is unsupported. No test examples were selected. The unfiltered test set averaged -0.216R across 2,043 examples in 33 days. Avoiding trades here is not proof of a profitable selection model.

Artifacts: `backend/ml_artifacts/scalp_returns_20261006/20261006112643193036/` contains `model.joblib`, `report.json` and `test_predictions.csv`. The report records the input dataset SHA-256. This historical test period was already inspected in earlier experiments, so the new results are retrospective and require fresh forward evaluation. The day bootstrap is approximate and does not remove every dependence or multiple-selection bias.

Four checks passed for rejecting negative returns, rejecting tiny selections, accepting a sufficiently broad positive synthetic validation example, and reporting zero selections without invented performance. These test fixtures are not training data. Model 92 remains inactive. Active advisory model 91, trading mode and execution controls were not changed. No model can guarantee positive trades.


## Expanded direct market-data return training: 6 October 2026

Rebuilt the dataset directly from **190,776 recorded one-minute NIFTY/BANKNIFTY candles**, generating **28,332** simulated long/short examples. Source coverage runs from August 2025 through October 2026. No synthetic prices or completed-trade records were substituted for market candles. Labels simulate next-open entry, twelve-minute horizon, 0.6 ATR stop, 1R target and the assumed 0.1R cost. Spot-index volume remains unavailable.

Dataset command: `backend/.venv/Scripts/python.exe backend/manage.py train_scalp_market --limit 100000 --output-dir backend/ml_artifacts/scalp_market_full_20261006`.

Return training command: `backend/.venv/Scripts/python.exe backend/manage.py train_scalp_returns --dataset backend/ml_artifacts/scalp_market_full_20261006/20261006112958863433/dataset.csv --output-dir backend/ml_artifacts/scalp_market_returns_full_20261006`.

Whole-day splits: 16,930 training examples across 154 days, 5,808 validation examples across 52 days and 5,594 test examples across 52 days. The historical test period overlaps previous experiments; results are retrospective, not fresh forward validation. Long/short paired examples are correlated.

New inactive return-model registry ID **94**, chosen estimator **robust_gradient_boosting**. Selection cutoff: None. Positive-return selection support: False. Reason: no validation cutoff passed positive-return confidence gate. Test selections: 0; mean selected net R: None. The full test baseline mean net R was -0.1291 under the assumed cost.

Artifacts: `E:\algo_trading\claude ai algo trading platform\algo_platform\backend\ml_artifacts\scalp_market_returns_full_20261006\20261006113032201619` contains `model.joblib`, `report.json` and `test_predictions.csv`. Source market dataset and classification artifact are stored under `backend/ml_artifacts/scalp_market_full_20261006/20261006112958863433/`. Active advisory model 91 and execution settings were not changed. Neither training nor a positive historical result would guarantee winning trades or real option profitability.


## Learning from executed paper trades: 6 October 2026

A separate append-only `RealizedTradeFeedback` ledger now ingests completed option trades from `PaperTrade` and closed paper `OpenPosition` records. It does not train from hypothetical HOLD/SKIP decisions or candle-label examples. A unique source/ID constraint prevents duplicate learning. Entry-time numeric features are whitelisted; realized profit, exit prices and MFE/MAE cannot become predictors. Invalid risk or entry timing is rejected. Paper and live sources are not mixed; only paper is collected today.

Net R is recorded net profit divided by initial quantity-adjusted risk. The paper AI ledger uses immutable `initial_stop_loss`; the execution ledger uses the originating signal stop, not a trailed position stop. Paper financial results and charges remain modeled rather than real broker results. Repeated collection does not rewrite the original feedback snapshot.

Collection runs every five minutes. Candidate retraining runs weekdays at 18:15 Asia/Kolkata while Celery worker and Beat remain running. Training requires at least 100 completed trades across thirty entry days and passes whole-day, overlap, finite-feature and selection checks. It learns expected recorded net R, evaluates return cutoffs on validation only and registers an inactive challenger; it does not automatically promote or change risk rules.

The initial import captured three completed paper trades over one day, all losses, with net results -678.22, -567.59 and -1670.98 INR. Candidate training was skipped (`insufficient_taken_trade_history`). These three examples are not enough to demonstrate a reliable learned improvement.

View live status at `/learning` under **Learning from taken trades**, backed by `/api/learning/trade-feedback/`. The panel displays sample count, profit/loss counts, costs, net P&L and the latest training result. Nine feedback/selection tests passed, plus the frontend build. Active advisory model 91 and execution settings remain unchanged. More paper outcomes and forward evaluation are required before claiming profitable trade selection.


## Internet historical-data expansion: 6 October 2026

Downloaded real historical one-minute NIFTY and BANKNIFTY candles from the configured Angel One SmartAPI account. Source documentation: https://smartapi.angelone.in/docs (historical endpoint, one-minute requests limited to 30-day windows). No external news text, invented prices, or daily bars were substituted for minute history.

Command: `backend/.venv/Scripts/python.exe backend/manage.py backfill_historical_candles --timeframe 1m --days 730`.

All 50 request windows completed. Added 176,868 distinct candles (88,434 per symbol). Stored totals are 183,828 NIFTY and 183,820 BANKNIFTY rows: 367,648 overall, from 7 October 2024 through 6 October 2026. Counts exclude duplicate rows; the backfill command's prior `bulk_create(ignore_conflicts=True)` return-list count was corrected because it included attempted duplicates. Empty successful windows are not proof of complete historical coverage.

Training command: `backend/.venv/Scripts/python.exe backend/manage.py train_scalp_market --limit 200000 --output-dir backend/ml_artifacts/scalp_internet_2y_20261006`.

Generated 54,638 simulated long/short examples. Day-separated splits: 32,712 training examples over 296 days; 11,058 validation examples over 99 days; 10,868 test examples over 99 days. Entry is next-minute open, horizon twelve minutes, stop 0.6 ATR, target 1R, assumed cost 0.1R. Paired long/short examples are correlated. Spot-index volume remains unavailable. This expands the scalping dataset; BTST remains rule-based.

Probability candidate **95** is inactive (gradient_boosting). Held-out AUC 0.5521; Brier 0.2495 versus baseline 0.2499. The fixed 0.60 cutoff selected zero test trades. Artifacts: `E:\algo_trading\claude ai algo trading platform\algo_platform\backend\ml_artifacts\scalp_internet_2y_20261006\20261006124218246528`.

Return candidate **96** is inactive (robust_gradient_boosting). No validation cutoff passed the positive-return confidence gate; threshold remains null and test selections are zero. Unfiltered test mean -0.1246R, profit factor 0.7801. Artifacts: `E:\algo_trading\claude ai algo trading platform\algo_platform\backend\ml_artifacts\scalp_internet_returns_2y_20261006\20261006143301398078`. Return training used the classification folder's `dataset.csv` via `train_scalp_returns`.

More history has not demonstrated profitable selection. The test period overlaps previously inspected historical experiments and is retrospective; fresh forward evaluation is still required. Active advisory model 91 and execution mode were not changed. Ten labeling/return-gate regression tests passed.


## Paper entry cooldown repair: 7 October 2026

The consecutive-loss entry gate now expires after `AI_PAPER_COOLDOWN_MINUTES` from the latest closed trade when the loss streak reaches `AI_PAPER_MAX_CONSECUTIVE_LOSSES`. Previously, three historical losses blocked all future paper entries indefinitely, because only a winning trade could clear the streak. Each new loss while the streak remains above the limit starts another cooldown. Daily loss, maximum drawdown, liquidity, one-position, and cash constraints remain enforced. Qualified paper entries continue through the existing order/exit/feedback pipeline; no live trading is enabled.


## Feasible paper entries and immediate learning feedback

Paper-AI contract selection first checks the preferred strike, then up to nine ranked alternatives within the selector delta sweet spot. Every candidate must pass the same pre-entry risk, cash, liquidity and quote-freshness checks. This does not round an unaffordable quantity up, weaken confidence gates, or modify the separate execution pipeline. If no candidate qualifies, the decision remains SKIP.

After a paper trade settles and its entry decision is linked to the outcome, an after-commit callback queues feedback collection. The five-minute collector remains the retry path and duplicate feedback remains prevented by source/trade identity. Candidates still require 100 trades over 30 days before retraining and remain inactive pending evaluation. This improves collection and feasible contract selection; it does not establish improved profitability or guarantee entries.
