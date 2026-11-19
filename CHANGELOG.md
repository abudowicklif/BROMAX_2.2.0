## Focused signal desk
- Replaced the detailed dashboard tables with a single combined-candidate view, compact market status, and concise signal-integrity checks.
- Removed agent execution details, per-strategy forecasts, and forecast history from the dashboard while retaining background processing and API access.
- The dashboard now polls only health, latest tick, and current signals; stale or mismatched candidate digits remain hidden.

## Combined strategy candidate
- Added a distinct equal-weight mean of the ten registered strategies' probability vectors for each complete, fresh, consistent tick.
- Exposed the `UNVALIDATED_CANDIDATE` separately in `/signals`, `/decision`, and the dashboard; incomplete, invalid, stale, or inconsistent inputs do not display a candidate digit.
- Attached agent evidence only when all eight agent results match the candidate tick; kept ensemble validation, correlation review, calibration, and trading authorization pending/disabled.
- Added focused aggregation, tie, fail-closed data, API, and dashboard tests.

## Continuous agent operation
- Run all eight deterministic agent roles on each completed live strategy batch, even when research, data-quality, or probability evidence is pending or rejected.
- Keep exploratory agent findings separate from validation and paper-signal qualification; preserve existing fail-closed signal and execution gates.
- Expose per-agent execution/result/validation status, runtimes, findings, metrics, and actual in-process coordination handoffs through health and the compact dashboard.
- Isolate and record individual agent exceptions so remaining specialists and the coordinator can complete the cycle.

## Background proposal accounting
- Bounded concurrent optional Deriv proposal connections with the collector's existing semaphore.
- Moved quote, paper-gate, and duration-ledger writes to a serialized worker thread so they cannot synchronously stall live tick processing.
- Added a collector test that proves the event loop remains responsive while quote-ledger persistence is blocked.

## Dashboard clarity and refresh efficiency
- Separated the latest-tick strategy batch from per-tick forecast history and collapsed each historical batch to its highest-probability research candidate.
- Refreshed live pipeline/current forecasts every three seconds and the history table every 30 seconds.
- Removed unused volatility and paper-trading placeholders; distinguished an in-progress analysis from stale market data.

## Live tick pipeline performance
- Added an epoch-aware forecast index for immediate next-tick scoring and chronologically ordered signal-history reads.
- Cached the collector's existing 1,000-tick context in memory and refresh it after Deriv history backfills; strategy inputs remain unchanged.
- Persisted each tick's existing strategy forecasts in one SQLite transaction instead of one transaction per strategy.
- Added bounded monotonic stage timings and a lightweight `/performance` endpoint; no strategy, probability, or qualification rules changed.
- Delivered the already-persisted latest-tick forecast batch through `/signals` instead of recalculating it per request; incomplete batches fail closed as `analysis_pending`.

## Live market pipeline visibility
- Added per-symbol connection, subscription, tick-quality, analysis, strategy, signal-generation, freshness, and error state to collector monitoring.
- Reconnects after both network failures and normal WebSocket closure, then restores history context and tick subscriptions.
- Backfilled older missing history without retrospective forecasts; all tick-history readers sort by market epoch.
- Ignored duplicate epochs, dropped delayed out-of-order live ticks, and counted epoch gaps without disrupting the stream.
- Added `/signals/history` for persisted strategy forecasts and surfaced pipeline status and signal history in the primary dashboard.
- Kept existing strategy calculations, quote handling, signal qualification, and paper-only execution boundaries unchanged.
- Added focused collector, reconnect, API, and dashboard tests.

## Live-execution safety foundation
- Added a process-wide, default-engaged, one-way execution kill switch and `POST /safety/kill-switch`.
- Kept live execution unimplemented and disabled; the endpoint cannot clear the switch or place orders.
- Added tests confirming the guard blocks execution and the API reports live execution remains unavailable.

## Deterministic multi-agent decision snapshot
- Added eight typed specialist/coordinator roles that reuse existing research, calibration, volatility, quote, risk, and paper-ledger calculations; no LLM output can generate probabilities or authorize trades.
- Added `GET /decision` as the synchronized source for the minimal dashboard and Windows floating panel, including same-tick consistency checks and fail-closed signal approval.
- Added immutable, throttled agent-run persistence and focused tests for data quality, no-trade vetoes, persistence, and snapshot synchronization.

## 2.2.1
- Changed the local BROMAX web/API listening port from 8000 to 8765 to avoid the previously occupied port. Updated launch command and dashboard URL.
- Replaced the primary dashboard with a compact five-section decision terminal; retained the detailed dashboard at `/advanced`.
- Added a fail-closed signal card that requires both the existing READY checks and the stricter per-digit evidence decision.
- Reduced the always-on-top Windows panel to the same six decision fields and kept polling off the Tkinter UI thread.
- Added holdout-derived current-regime volatility health and rejection reasons without changing research selection or approval statistics.
- Added ten-digit research cards, evidence-quality health scores, duration breakdowns, and paper-performance breakdowns to the dashboard and `GET /digits/analysis`.
- Added a synchronized Windows floating panel with always-on-top and compact modes; the standard launcher opens it with the dashboard.
- Kept missing model estimates blank and trade decisions fail-closed until per-digit evidence, quote, timing, manual-approval, and paper-risk checks pass.
- Added focused API, forecast-ordering, and per-digit research coverage; documented the endpoint and panel.

# BROMAX 2.2.0 — 18 November 2026

Earlier 2.1.0 release notes are retained below for traceability.

## Multi-tick timing research
- Added a dedicated 1–5 tick horizon evaluator that scores frozen forecasts against the digit exactly N observed ticks later. Missing 1-second epochs are excluded rather than silently shifted.
- Added chronological development (60%), validation (20%), and untouched final-test (20%) splits, Wilson confidence intervals, and Holm multiple-comparison correction across strategy-duration candidates.
- Reports a research lead only when validation and final-test hit-rate evidence pass minimum-sample and significance gates. This is not an execution approval.

## Volatility diagnostics
- Added trailing 20-return absolute-volatility measurements and development-trained tercile regimes (LOW/MID/HIGH).
- Displays regime-specific sample sizes and holdout results alongside timing candidates.
- Added a fail-closed volatility gate to the READY panel: a one-tick signal is rejected unless the matching strategy and current volatility regime pass minimum-sample, untouched final-test confidence-bound, and Holm-adjusted significance checks. This can intentionally leave the system at NO TRADE until enough evidence exists.

## Safety and limitations
- Added `GET /timing?symbol=...` and a dashboard timing/volatility research panel.
- The existing paper executor remains one tick. Multi-tick execution is deliberately not enabled because BROMAX does not yet capture duration-specific quotes or validate payout-adjusted EV for 2–5 tick contracts.
- Paper-only mode remains; no live order endpoint, authentication, or buy/sell capability was added.
- No timing can be described as guaranteed or worth risking essential funds on. If evidence is insufficient, the result is no validated timing.


## 2.2.0 — 2026-11-18
- Added live read-only Deriv proposal quote capture for 1–5 tick MATCH durations using batched WebSocket requests.
- Added append-only duration quote and settlement ledger with exact target-epoch matching.
- Added actual payout-adjusted realized P/L evaluation, chronological holdouts, HAC confidence intervals, and Holm correction.
- Added quote-backed profitability panel to the existing new dashboard.
- Added a fail-closed paper-entry gate requiring confirmed profitability for the exact one-tick executor strategy.
- Kept live execution disabled; multi-tick durations remain research-only until duration-aware paper execution is built.
