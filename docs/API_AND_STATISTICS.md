# BROMAX technical notes

## Contract definition

This protocol is fixed to `DIGITMATCH`, one tick (`duration=1`, `duration_unit="t"`), and the `1HZ100V` default symbol. The source videos showed both one- and two-tick configurations; BROMAX intentionally does not pool those horizons. A different duration requires a separately versioned study and outcome-scoring rule.

## Forecast timing invariant

For tick `t`, the collector stores the tick, scores only forecasts whose `forecast_epoch == t.epoch - 1`, builds `ForecastContext` from history ending at `t`, and appends forecast events before returning to the WebSocket reader. The context has no field for the next tick. If the next one-second epoch is absent, the prior forecast remains unscored.

Proposal quotes are requested asynchronously after the forecast event has been appended. The quote is an additional immutable event linked to the forecast sequence. The paper bot refuses to open a paper position if the quote arrives after a later tick has already been stored.

## Append-only model

`audit_events` contains `FORECAST`, `SCORE`, `QUOTE`, `PAPER_OPEN`, and `PAPER_SETTLE` events. SQLite triggers reject updates and deletes. A forecast is never rewritten when a quote or result arrives; related events are appended and linked by the forecast/event sequence in `note`.

## Expected value

The Deriv proposal response's `payout` is treated as total payout including stake, and `ask_price` as quoted stake. For predicted digit probability `p`, quoted total payout `P`, and stake `S`:

`Model EV = p * P - S`

`Realized contract P/L = (P - S) if hit, otherwise -S`

`break_even_probability = S / P`

The promotion gate uses the lower bound of the realized P/L moving-block interval, not model EV alone.

No quote means no EV observation. The quote is digit-, symbol-, duration-, currency-, stake- and time-specific.

## Statistical interpretation

The exact one-sided binomial test is against the uniform 10% null. Holm correction controls family-wise error across the pre-registered validation candidates. Wilson intervals and 50-tick moving-block bootstrap intervals are reported for hit rate. The exact binomial p-value assumes independent hit events; the block interval is included to expose sensitivity to serial dependence. Multiclass Brier score and top-probability reliability bins evaluate probability quality. Realized P/L intervals use a moving-block bootstrap (50-tick blocks) to retain some local serial dependence. Model-EV intervals are reported separately. These intervals remain estimates, not guarantees.


## Intelligence layer (protocol 1.1.0)

The live dashboard is served at `/dashboard`. `/intelligence` exposes descriptive digit frequencies, order-1/2/3 conditional recurrence, a smoothed transition matrix, run length, a chi-square uniformity diagnostic, and simple absolute-return summaries. `/signals` computes the pre-registered candidate probability vectors from the latest observed history. These endpoints are diagnostic only: they do not provide calibrated confidence by themselves and do not authorize paper trading.

The order-2 transition candidate is evaluated as part of the fixed strategy family. It estimates successor frequencies for the latest two-digit context with symmetric Laplace smoothing. If fewer than 20 historical matches exist, it falls back to Laplace-smoothed frequency over the last 500 digits. The threshold and fallback are fixed in protocol 1.1.0. Conditional frequencies selected from the same history are exploratory; only chronological validation, untouched-test evidence and actual proposal payout accounting can qualify a candidate.

At startup, BROMAX requests up to 1,000 historical ticks per configured symbol using `ticks_history`, reads the response `pip_size`, and stores the ticks as context only. It does not create retrospective forecasts or paper trades from the seed. It then starts the live `ticks` subscription. Deriv documents the public WebSocket endpoint and tick-history fields in its [public WebSocket documentation](https://developers.deriv.com/docs/options/ws-public/), [tick comparison/schema](https://developers.deriv.com/comparison/ticks/), and [ticks-history documentation](https://developers.deriv.com/docs/data/ticks-history/).

Protocol snapshots are versioned. A 1.1.0 report does not overwrite a previously frozen 1.0.0 snapshot, and paper approval requires a frozen snapshot whose protocol version matches the running paper bot.

## Dashboard signal readiness endpoint (2.0.0)

`GET /signals/ready?symbol=1HZ100V` is a read-only view of the most recent forecast from an explicitly approved strategy. It reports `READY` only if the frozen research gate, actual timely proposal quote, positive quote-adjusted expected value, one-tick expiry, and paper-risk limits all pass. Other outcomes return `WAITING`, `REJECTED`, or `EXPIRED` with an explanation. The endpoint never submits a Deriv contract and never invokes a live order API.


## Quote-backed duration profitability (2.2.0)

`GET /timing?symbol=...` now includes `payout_optimization`. The collector requests read-only Deriv `DIGITMATCH` proposals for horizons 1–5 ticks for each distinct candidate digit. Each accepted quote is immutable and tied to its forecast sequence, prediction, symbol, strategy version, actual stake, gross payout, proposal ID, and `spot_time`. The quote is accepted only if the proposal spot matches the forecast epoch and it arrives before the next tick deadline.

A duration outcome is settled only at `forecast_epoch + duration_ticks`; missing epochs are not replaced by later ticks. Net P/L is `payout - stake` on a hit and `-stake` on a miss. The optimizer reports total stake/P&L, mean return on stake, 95% HAC confidence intervals (five Newey-West lags), one-sided tests for positive mean return, and Holm-adjusted p-values across all strategy-duration candidates. Validation selects; the final 20% chronological holdout is used once for confirmation. Each validation and final-test segment requires at least 5,000 settled quote-backed contracts.

`GET /signals/ready?symbol=...` uses the payout-optimized strategy-duration pair, a fresh matching proposal, positive current model EV, strategy approval, strategy/duration-specific volatility confirmation, and paper risk checks. It never submits an order. The dashboard displays the quote-backed duration research alongside the earlier hit-rate and volatility diagnostics.

Historical Deriv proposal prices cannot be reconstructed from historical tick prices. Therefore payout evidence starts prospectively and can remain insufficient for a long time. The passive duration ledger simulates every quoted candidate for research; the separate risk-limited paper account ledger continues to record one-tick entries only.

## Digit evidence dashboard and floating panel

`GET /digits/analysis?symbol=1HZ100V` returns exactly ten digit rows. Model probability is `null` when no selected model estimate exists; historical frequencies and performance are likewise `null` until observations exist. Each row includes validation and untouched-test summaries, Holm-adjusted per-digit hit tests, support and quote coverage, block-bootstrap intervals, walk-forward folds, volatility-regime summaries, and an evidence-health score.

The 0–100 health score summarizes evidence quality and is not a win probability. A per-digit decision is fail-closed: it requires the selected strategy to be a validation finalist, at least 1,000 scored and quote-settled observations in each holdout, at least 90% quote coverage, Holm-adjusted significance, hit-rate lower bounds above 10%, positive realized-profit confidence bounds in both holdouts, and evidence in every volatility regime. The API adds fresh quote, duration, explicit approval, and paper-risk checks. No value is fabricated to fill missing evidence.

On Windows, `python -m bromax.desktop` opens the synchronized Tkinter panel for a running local API. It supports always-on-top and compact/expanded modes. `INSTALL_AND_RUN.bat` starts it alongside the browser dashboard. Authentication credentials can be supplied with environment variables or entered into the panel prompt; the prompt keeps them in memory only. Neither display client exposes live order placement.

## Minimal decision terminal

`GET /dashboard` is the primary compact screen: market freshness, all ten digit estimates and evidence scores with rejection reasons, one signal that must pass both `/signals/ready` and per-digit qualification, the current volatility filter, and recorded paper results. Probabilities and health remain blank when the API has no corresponding calculated result. The volatility health percentage is the fraction of six observed current-regime holdout checks passed (validation and final-test sample minimum, hit-rate lower bound, and Holm-adjusted significance); it is an evidence score, not a probability.

The previous detailed dashboard is preserved at `GET /advanced`. The floating Windows panel shows only the same recommended digit, probability, health, duration, volatility approval, and paper-only trade status.
