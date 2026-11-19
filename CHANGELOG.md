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
