# BROMAX 2.0.0 — Deriv MATCH research, signal desk and paper bot

**Safety boundary:** BROMAX is paper-only. It connects to public market data and requests read-only price proposals. It has no API-token handling, no account authentication, and no `buy`, `sell`, or live order-placement implementation. No live-execution endpoint is exposed.

BROMAX 1.1.0 is a research system, not a promise of profit. A strategy is not approved merely because it wins often. It must pass a pre-registered validation gate using the actual quoted payout and then survive an untouched final test. The default approval file is absent, so the paper bot does not open any paper positions until a strategy has been explicitly approved by the research process.

## 1. Intelligence-to-bot integration (2.0.0)

BROMAX is one connected application, not two independent predictors. The Deriv collector feeds the intelligence engine; the engine freezes forecasts before the next tick; the proposal client requests a read-only quote for the exact predicted digit; the paper bot receives that same forecast, quote, and forecast ID. It checks explicit strategy approval, frozen research evidence, payout-adjusted expected value, quote timing, current tick identity, stake ceiling, daily loss cap, and trade-count limit before opening a simulated position. The next tick settles that exact forecast. The `/bridge/status` endpoint exposes the handoff state. The dashboard/API can monitor it at `/bridge/status`.

**No live trading:** this build intentionally contains no API token login, no account authorization, and no `buy`/`sell` request. It is fully runnable in live-feed + read-only-quote + paper-execution mode. Because no strategy has been validated and explicitly approved by default, a fresh installation starts in observation/research mode and does not fabricate trades.

The compact browser dashboard uses lightweight `/health`, `/signals`, `/signals/primary`, and `/signals/history` endpoints so live forecasts remain visible without waiting for full research reports. A single background worker checks fresh ticks once per second, runs inexpensive preflight gates first, and starts full eight-agent qualification only when those gates pass; full analysis is limited to once per symbol per minute and never overlaps another decision run. The primary panel only displays a cached qualification that matches the latest fresh tick; otherwise it explicitly shows `NO QUALIFIED SIGNAL`. Strategy forecasts and probabilities remain research-only and never authorize trades.

## 2. What was inspected and changed

The available earlier project was `deriv_matches_starter.zip`, containing `app/main.py`, `README.md`, `requirements.txt`, and `.env.example`. A separate `app.py` and the `quant-system-prod` repository itself were not present in the available uploads or Library search results; only a directory listing mentioning that ZIP was available. I therefore inspected and extended the available `app/main.py` starter. The specific `int(round(value)) % 10` expression you mentioned was not present in that inspected file, so I do not attribute it to that source.

### Bugs and weaknesses addressed

1. **Precision was only provisional.** The starter `quote_details()` inferred decimal places from the serialized quote string and did not read `tick.pip_size`. It did preserve `Decimal` and did not explicitly strip zeroes, but it could not restore zeroes absent from the JSON number. BROMAX formats each quote to Deriv's `pip_size` precision before extracting the last displayed decimal digit. It never routes quotes through binary floats.
2. **Database lifecycle.** The starter ran `CREATE TABLE IF NOT EXISTS` on each connection, and its `with sqlite3.connect(...)` usage committed transactions but did not explicitly close the connection. BROMAX initializes the schema once, uses WAL/indexes, and closes each connection through a dedicated context wrapper.
3. **Single-symbol state.** The starter used one global symbol and an index only on epoch. BROMAX accepts multiple symbols and every history/forecast query is symbol-scoped.
4. **No deduplication or audit immutability.** The starter had no unique tick key and no append-only triggers. BROMAX deduplicates 1-second ticks by `(symbol, epoch)` (not `tick.id`, which Deriv defines as a per-connection unsubscribe identifier), rejects out-of-order live ticks, records epoch gaps, and prevents updates/deletes to tick, event and frozen-snapshot records.
5. **No forecasts or outcome linkage.** BROMAX creates an immutable forecast event before the next tick is processed, then appends a separate score event against the immediately following one-second epoch only. If a tick is missed, the forecast is left unscored; it is never scored against a later tick.
6. **No strategy versioning.** Every forecast records strategy ID, semantic version, exact parameter JSON, prediction, full 10-digit probability vector, forecast tick ID and timestamp.
7. **No payout accounting.** BROMAX requests a `DIGITMATCH` proposal for the predicted digit, stake, currency and one-tick duration. The proposal is recorded as a separate append-only quote event, including Deriv’s `spot_time` for audit. Missing quotes are never replaced with assumed payouts; those forecasts cannot contribute to EV or open a paper position.
8. **No statistical controls.** BROMAX adds chronological development/validation/final-test splits, Wilson hit-rate intervals, one-sided binomial tests against 10%, Holm correction, multiclass Brier score, reliability bins, and moving-block bootstrap intervals for hit rate and realized payout-adjusted P/L.
9. **No paper execution boundary.** Paper decisions are separated from the research engine. The only settlement is simulated against the next tick. No real order API exists.
10. **Silent malformed-data handling.** Missing quote/epoch/pip_size, invalid precision, non-finite quote, and excess precision are rejected and logged. WebSocket failures reconnect with capped exponential backoff; health reports stale data and recent errors instead of always returning `ok`.

## 3. Project structure

```text
BROMAX/
├── .env.example
├── .gitignore
├── app.py                       # ASGI entry point (new wrapper, not the missing uploaded app.py)
├── README.md
├── FULL_SOURCE.md
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── bromax/
│   ├── __init__.py
│   ├── api.py
│   ├── agents.py                 # typed deterministic specialists and decision coordinator
│   ├── cli.py
│   ├── collector.py
│   ├── config.py
│   ├── digits.py
│   ├── intelligence.py             # conditional patterns, transition matrix, volatility diagnostics
│   ├── dashboard.html              # live monitoring UI
│   ├── paperbot.py
│   ├── proposals.py
│   ├── reporting.py
│   ├── research.py
│   ├── storage.py
│   └── strategies.py
├── docs/
│   └── API_AND_STATISTICS.md
├── data/
├── reports/
└── tests/
    ├── test_digits.py
    ├── test_leakage.py
    ├── test_agents.py
    ├── test_research.py
    ├── test_storage.py
    └── test_api.py
```

For the complete contents of every project source/configuration/test file in one readable listing, see `FULL_SOURCE.md`.

## 4. Pre-registered strategy set

The candidate list is fixed in `bromax/strategies.py` and reported in every JSON report. The cumulative-mode candidate is the explicit “current most-frequent digit” rule; rolling modes are separate fixed-window alternatives. Do not modify it after inspecting validation or final-test results.

| Strategy | Parameters | Rule |
|---|---|---|
| Uniform random | Deterministic SHA-256 seed per symbol/tick | Reproducible uniform guess; probability vector is 10% each |
| Cumulative mode | All prior observed digits | Most frequent digit across all collected history; lowest digit breaks ties |
| Rolling mode 100 | Last 100 digits | Select most frequent digit; lowest digit breaks ties |
| Rolling mode 500 | Last 500 digits | Same rule |
| Rolling mode 1000 | Last 1,000 digits | Same rule |
| Laplace frequency 100 | Window 100, alpha=1 | Dirichlet-smoothed digit probabilities; select argmax |
| Laplace frequency 500 | Window 500, alpha=1 | Same rule |
| Exponential frequency 50 | Half-life 50 ticks, alpha=1 | Exponentially decayed counts, symmetric smoothing |
| Exponential frequency 200 | Half-life 200 ticks, alpha=1 | Same rule |
| Conditional transition order 2 | Last two digits, alpha=1, minimum support 20 | Estimate the next digit from historical successors of the same two-digit context; below 20 matches, fall back to Laplace frequency over the last 500 digits |

The smoothed frequency methods generally preserve the mode under a symmetric prior; their added value is in probability estimation and calibration, not a magical different top digit. The conditional-transition method is a pre-registered candidate, not an assumed edge. Its low-support fallback is fixed in advance. No neural network is included.

## 4. Digit extraction

Deriv's tick response includes `pip_size`; the API schema describes it as the displayed decimal precision and demonstrates formatting with `toFixed(pip_size)`. BROMAX therefore uses `Decimal` and quantizes to that number of decimal places before reading the last character. For example, quote `1234.5` with `pip_size=2` becomes `1234.50`, and the displayed last digit is `0`.

The extractor rejects precision values outside 0–10, non-finite values, malformed quotes, and non-zero digits beyond the declared precision. It does not silently round malformed input.

## 5. Install on Windows

Python 3.11 or newer is recommended. The quickest route is to extract the ZIP and double-click `INSTALL_AND_RUN.bat`; it creates the virtual environment, installs dependencies, starts the API, and opens the dashboard. To install manually, use the commands below. On connection, the collector requests the latest 1,000 historical ticks from Deriv, stores them as context only (no retrospective forecasts or trades), and then subscribes to live ticks. This lets the research engine begin live forecasts without waiting 1,000 seconds. If historical seeding fails or times out, BROMAX continues with live ticks and waits until the configured 1,000-tick minimum is reached.

```bat
cd BROMAX
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e ".[test]"
copy .env.example .env
```

BROMAX loads the `.env` file automatically with `python-dotenv`; environment variables already set in the terminal take precedence. This protocol is restricted to `1HZ` one-second symbols because its outcome rule requires the immediately following one-second epoch. Defaults use symbol `1HZ100V`, currency `USD`, $1 paper stake, maximum paper stake $1, daily paper loss cap $10, and 100 paper contracts/day across all configured symbols. Set `CURRENCY` to the account-holder currency required by Deriv’s proposal API; BROMAX does not log in to discover it. No account login is performed. This is a public-feed paper simulator, not an authenticated integration with a Deriv demo account or its balance.

## 6. Run tests

```bat
pytest -q
```

The suite includes symbol-labelled precision fixtures with explicit `pip_size` values (including trailing zeroes); the production collector always uses the live tick’s own precision, a deliberate future-data access attempt that must raise `AttributeError`, append-only database checks, immediate-next-tick scoring, and statistical helper checks.

## 7. Run collector, API and paper bot

```bat
uvicorn app:app --host 127.0.0.1 --port 8765

# Equivalent: uvicorn bromax.api:app --host 127.0.0.1 --port 8765
```

The dashboard opens at `http://127.0.0.1:8765/dashboard`; the API docs are at `http://127.0.0.1:8765/docs`. You can also generate a report from the command line with `python -m bromax.cli report`. The CLI default writes the approval report to `reports/bromax_report.json`; the paper bot reads that fixed path.

- `GET /dashboard` — compact live-market terminal with the cached qualified primary signal (or a clear no-signal state), per-strategy forecasts, pipeline status, and persisted forecast history.
- `GET /advanced` — detailed research, strategy, timing, and historical dashboard.
- `GET /decision?symbol=1HZ100V` — one timestamped response including the eight deterministic agent results. It fails closed if the latest tick changes during evaluation and returns HTTP 503 rather than overlapping the bounded background analysis.
- `POST /safety/kill-switch` — one-way, process-wide execution stop. It starts engaged, can only be engaged (never cleared through the API), and does not add live execution.
- `GET /health` — WebSocket and subscription state, per-symbol tick/forecast counts, last quote, data freshness, analysis/strategy/signal-generation state, background primary-analysis status, quality counters, and recent errors.
- `GET /performance` — rolling 256-tick monotonic timing summaries for JSON parsing, data processing, strategy evaluation, forecast persistence, and receive-to-forecast latency. Network transit is excluded.
- `GET /ticks/latest?symbol=1HZ100V` — latest raw/displayed quotes and digits.
- `GET /digits/distribution?symbol=1HZ100V&window=1000` — rolling empirical digit frequencies.
- `GET /intelligence?symbol=1HZ100V&window=1000` — conditional order 1/2/3 recurrence, transition matrix, digit-run length, uniformity diagnostic and descriptive price-volatility metrics.
- `GET /signals?symbol=1HZ100V` — the collector's persisted forecasts for the latest fully analyzed tick. It never recalculates a second, potentially divergent signal set; returns `analysis_pending` rather than stale/incomplete forecasts. Outputs remain research-only.
- `GET /signals/history?symbol=1HZ100V&limit=50` — persisted strategy forecasts with prediction probability, target epoch, scored hit/miss, and current/expired status. This read-only analysis history never places an order.
- `GET /forecasts?symbol=1HZ100V&limit=500` — most recent forecasts, scores and quote metadata (limit 1–5,000).
- `GET /report` — statistical report.
- `GET /paper/status` — paper limits and execution state.
- `GET /paper/trades?symbol=1HZ100V` — append-only paper open/settlement events.

There is no live-execution route and no buy/sell endpoint.

The collector stores data in `data/bromax.sqlite3`. Research final-test snapshots are stored write-once in the same database; new ticks after the frozen snapshot belong to the forward paper period, not a refreshed final test. Keep the process running to collect a meaningful sample. A live quote proposal is requested asynchronously for each distinct predicted digit (maximum three concurrent proposal connections and 20 pending quote tasks; if the queue fills, forecasts are retained but payout requests are skipped). Quote, paper-gate, and duration-ledger writes run in a serialized worker thread so optional quote processing does not block the collector's WebSocket event loop. The proposal `spot_time` must equal the forecast tick epoch, and the quote must be recorded before the next tick; otherwise no paper position is opened and the payout is excluded. The forecast remains in the log with missing payout and is excluded from payout-adjusted EV.

## 8. Research protocol and report interpretation

Chronological split: first 60% development, next 20% validation, final 20% untouched test. Four chronological walk-forward diagnostic folds are also reported over development+validation only; they do not inspect the final test. The contract is fixed to `DIGITMATCH` with a one-tick duration for this protocol. The videos show both one- and two-tick settings; BROMAX does not mix them. A two-tick study would require a separate protocol/version and a different scoring rule. The strategy candidate list, windows, decay half-lives and conditional-pattern settings are pre-specified. Development ranks candidates by multiclass Brier score. Validation is used for finalist selection. Final-test metrics are calculated only for validation finalists and are never used to retune. The first report that opens a final test stores an immutable per-symbol, per-protocol-version research snapshot in SQLite; later report calls reuse it and do not re-test on newly arriving ticks. Protocol 1.1.0 uses a separate versioned snapshot table, so it does not overwrite or reinterpret an older 1.0.0 snapshot. Both snapshot stores are write-once and protected by update/delete triggers.

A validation finalist must have all of the following:

- at least 5,000 scored validation forecasts and at least 5,000 quoted/scored contracts;
- at least 90% quote coverage for that strategy in the validation period;
- Holm-adjusted one-sided binomial p-value below 0.05 versus the 10% uniform benchmark;
- lower bound of the 50-tick moving-block bootstrap interval for realized payout-adjusted profit greater than zero;
- lower bound of the 50-tick moving-block bootstrap hit-rate interval greater than 10%;
- actual proposal payout and stake captured for the corresponding forecast.

If no strategy passes, the report says no finalist and does not open the final test. That is not a software failure; it means the data do not justify further claims. Even a positive final test is provisional and must be followed by a new forward paper period.

### Metrics

- `hit_rate_ci95`: Wilson interval for the hit rate (independent Bernoulli approximation).
- `hit_rate_block_bootstrap_ci95`: 50-tick moving-block bootstrap interval, used as an additional serial-dependence-aware gate.
- `p_vs_uniform_10pct_one_sided`: exact binomial test against the theoretical 10% uniform-guess benchmark.
- `holm_adjusted_p`: family-wise multiple-comparison correction across candidates in development, validation, and final-test finalist sets, separately within each symbol.
- `brier_multiclass_10class`: lower is better; proper score for the full ten-digit probability vector.
- `reliability`: top predicted probability bins, mean predicted probability and observed top-digit hit rate.
- `mean_break_even_hit_rate`: average `stake / total_payout` from the actual quote.
- `mean_realized_profit_per_contract`: observed contract P/L using the actual next-tick hit/miss and the quoted payout.
- `realized_profit_ci95`: moving-block bootstrap interval for realized P/L; this is the payout-adjusted evidence gate.
- `mean_model_expected_value` and `model_expected_value_ci95`: probability-model EV, shown separately and not substituted for realized P/L.

For a quoted total payout `P` including returned stake, stake `S`, and win probability `p`, expected value is `p*P - S`, and break-even is `S/P`. Do not use a fixed assumed payout. Proposal payout may vary by digit, duration, symbol, currency, stake and quote time.

### Sample report format (illustrative schema only; no market results fabricated)

```json
{
  "symbol": "1HZ100V",
  "strategy_id": "rolling_mode_500",
  "validation": {
    "n": null,
    "hit_rate": null,
    "hit_rate_ci95": [null, null],
    "hit_rate_block_bootstrap_ci95": [null, null],
    "p_vs_uniform_10pct_one_sided": null,
    "holm_adjusted_p": null,
    "brier_multiclass_10class": null,
    "reliability": [],
    "payout_adjusted": {
      "n_with_actual_quote": 0,
      "quote_coverage": 0.0,
      "mean_break_even_hit_rate": null,
      "observed_hit_rate_minus_mean_break_even": null,
      "hit_rate_minus_break_even_ci95": [null, null],
      "mean_realized_profit_per_contract": null,
      "realized_profit_ci95": null,
      "mean_model_expected_value": null,
      "model_expected_value_ci95": null
    }
  },
  "final_test": null,
  "final_test_verdict": null,
  "verdict": "inconclusive",
  "verdict_reason": "insufficient scored observations or proposal quotes"
}
```

Verdicts must distinguish **no evidence of edge**, **inconclusive**, and **evidence of edge**. A short run with low power is inconclusive, not proof of randomness. A negative EV interval is evidence against this strategy under the observed quote conditions. A positive validation result is not enough: require the untouched test and forward paper test.

## 9. Data sufficiency and power

The hard minimum of 5,000 validation forecasts is a gate, not a universal power guarantee. For scale, a one-sided 5% test with 80% power needs approximately 11,620 independent observations to distinguish 10% from 11%, or about 3,026 to distinguish 10% from 12%, before any multiple-testing penalty. Serial dependence and Holm correction can increase those requirements. The 5,000-observation gate is therefore a floor, not a guarantee of adequate power. Always publish `n`, confidence intervals and corrected p-values. If the report has fewer than 5,000 validation observations per strategy, no strategy can pass the current gate.

## 10. Paper bot safety

- Default approval file `approved_strategies.json` is absent: no strategy is approved.
- Paper stake is bounded by `MAX_PAPER_STAKE`.
- Daily settled paper loss is bounded by `DAILY_LOSS_CAP` (UTC day); open paper stakes are reserved at full stake as worst-case loss before another paper contract is allowed.
- Daily trade count is bounded by `MAX_TRADES_PER_DAY` (UTC day).
- A paper contract is opened only if a strategy ID appears in the user approval file, the machine-generated report confirms positive realized-profit confidence bounds on both validation and untouched test, the proposal quote exists, the proposal `spot_time` matches the forecast epoch, the quote arrives before the next tick deadline, the forecast probability implies positive EV at that exact quote, and limits pass.
- Settlement uses only the immediately next 1-second tick. Missed ticks are not substituted.
- No real account credentials are accepted or stored.

Do not manually create an approval file merely to force trading. After reviewing a frozen report, explicit user approval is represented by a file such as `approved_strategies.json` containing `{"approved":{"1HZ100V":["rolling_mode_500"]}}`; replace the example strategy with one actually listed as a validation finalist. The bot independently rechecks the stored report and refuses approval unless the untouched-test sample and quote counts are each at least 5,000, quote coverage is at least 90%, the block-bootstrap hit-rate lower bound is above 10%, the realized-P/L lower bound is above zero, and the Holm-adjusted p-value is below 0.05. The report generator does not silently promote a strategy.

## 11. Requirements before any future live component

A future live component would require a separate design review and explicit user decision, including secure credential storage (never source code or chat), explicit account/contract authorization, account-currency verification, proposal refresh and price-slippage handling, strict daily and per-contract loss limits, max exposure limits, idempotent order handling, audit logs, and a physical/software kill switch. None of these live execution functions are implemented here.

## 12. Known scope limitations

- The code's exact-next-tick rule assumes the configured synthetic index emits one-second ticks with integer epoch increments. The tick response `id` is stored only as metadata (it is a connection/subscription identifier, not a unique tick key); deduplication is by `(symbol, epoch)`. It intentionally leaves forecasts unscored across missing seconds. If a different symbol has a different tick cadence, implement and test a symbol-specific continuity rule before using it.
- `active_symbols` metadata is not fetched automatically in this first deliverable. `pip_size` is taken from each tick response, which is required by Deriv's tick schema; changes in precision are applied per tick and recorded.
- Quote requests use the public WebSocket proposal endpoint documented by Deriv. If Deriv changes endpoint access or requires authorization for a given account/product, quote failures are surfaced and no payout is fabricated.
- The proposal request currency defaults to USD and must be changed if needed. Deriv’s current API returns `ask_price`, `payout` and `spot_time` on a one-time proposal; its ID is informational and cannot be used to buy. BROMAX uses that response only as a quote and never attempts a purchase. Money amounts are serialized as exact decimal JSON numbers, not binary floats.
- Moving-block bootstrap uses 50 consecutive forecast observations per block to retain some local dependence. This does not model every possible long-range dependence or regime change; treat the intervals as evidence estimates, not guarantees.


## 2.0 Dashboard and READY signal panel

The dashboard at `/dashboard` is a working view over the live API and stored records, not a static mockup. It includes feed health, selected-market quote and last digit, tick count, digit distribution, candidate strategy forecasts, paper-execution controls, performance from settled paper contracts, activity, recent ticks, paper trade history, and research status.

The **Ready to trade** panel reads `GET /signals/ready?symbol=1HZ100V` (replace the symbol with one configured in `.env`). It shows `READY` only when an explicitly approved strategy has qualifying frozen out-of-sample evidence, a timely proposal quote for the exact predicted digit, positive payout-adjusted expected value, a still-valid one-tick entry window, and passing paper risk limits. Otherwise it explains why the system is waiting or rejecting the candidate. The endpoint is read-only; the collector and existing `PaperBot` remain responsible for paper execution. It does not initiate an order.

The visual design intentionally uses a restrained charcoal palette, compact data tables, uneven content widths based on information density, and operational language. No fake sample trades or placeholder performance metrics are injected. Empty states are explicit until live records exist.

### API

### Multi-tick timing and volatility research
- `GET /timing?symbol=1HZ100V` compares frozen per-tick forecasts over 1–5 tick horizons. It uses chronological 60/20/20 development, validation and untouched final-test segments, Wilson intervals, and Holm correction across strategy-duration candidates.
- The timing result is research-only. Even a final-test hit-rate lead is **not executable** until duration-specific Deriv proposal quotes establish payout-adjusted expected value and a forward paper test confirms settlements. The legacy risk-limited paper account executor remains one tick; the quote-backed duration ledger separately paper-simulates all 1–5 tick contracts.
- Trailing absolute-return volatility is bucketed using tercile cutoffs learned from the development segment. Bucket results are diagnostics only; volatility is not proof that a digit prediction is correct and does not authorize a trade.

## API addition

- `GET /signals/ready?symbol=1HZ100V` — read-only latest eligible signal and reason when not ready.

All previous research, forecast, quote, audit and paper endpoints remain available. The dashboard refreshes automatically every three seconds.


## BROMAX 2.2.1 — quote-backed duration profitability research

The timing engine now collects read-only Deriv `DIGITMATCH` proposal quotes for each distinct candidate digit at 1, 2, 3, 4 and 5 ticks over one WebSocket connection per forecast batch. It stores quote snapshots in a separate append-only duration ledger. A quote is eligible only when its `spot_time` equals the forecast tick and it arrives before the next one-second tick deadline. Each duration contract is settled only against the digit at the exact target epoch; missing ticks are not substituted.

The payout optimizer calculates realized net P/L from the actual quoted stake and gross payout, mean return on stake, chronological development/validation/final-test summaries, Newey-West/HAC confidence intervals for overlapping/serially dependent contracts, one-sided profitability tests, and Holm correction across strategy-duration candidates. Validation chooses candidates; the final test is held out. At least 5,000 quote-settled observations are required in each validation and final-test segment. The dashboard now shows quote coverage, the leading duration, realized P/L, mean return, confidence intervals, and evidence status.

Important constraints:
- Historical tick data cannot reconstruct historical proposal payouts. Quote-backed evidence begins accumulating prospectively after this version is running.
- A duration with the highest hit rate is not necessarily the most profitable; selection is based on actual quoted payout and realized P/L.
- No candidate is selected or execution-authorized until all statistical gates pass. The result may be NO TRADE indefinitely.
- The append-only duration ledger passively paper-simulates quote-backed outcomes for all 1–5 tick candidates. The legacy risk-limited PAPER_OPEN/PAPER_SETTLE account ledger remains one tick; the dashboard distinguishes these records. No live purchase is possible.
- Volatility remains a separate fail-closed confirmation filter. It is not a guarantee of the next digit.
- No live buy/sell endpoint, account authentication, Martingale, or real-money execution is present.


## Port configuration update (2.2.1)
The local API/dashboard server now listens on port **8765** instead of 8000. Open `http://127.0.0.1:8765/dashboard`. If 8765 is also occupied, change the `--port 8765` value in `INSTALL_AND_RUN.bat` and the matching browser URL on the next line.

## Digit evidence dashboard and floating panel

`/dashboard` is the compact live terminal: market status, current predictions from the existing strategies, pipeline status, and persisted signal history. Model probabilities are displayed as research-only estimates, not calibrated win chances or trade recommendations. The full qualification and digit-evidence analyses remain available through `/advanced` and `/digits/analysis?symbol=1HZ100V`; neither is required for continuous tick collection or strategy forecast generation.

On Windows, `INSTALL_AND_RUN.bat` opens both the browser dashboard and a synchronized floating panel. The panel shows only the recommended digit, probability, health, duration, volatility approval, and paper trade status; it can stay on top of other windows, resize, and open the dashboard. To run it separately while the API is already running:

```bat
.venv\Scripts\python.exe -m bromax.desktop
```

The panel reads the local API by default. For a password-protected API, set `BROMAX_AUTH_USERNAME` and `BROMAX_AUTH_PASSWORD` before starting it; otherwise, it prompts for credentials without saving them. The panel and dashboard are display clients only and cannot place live orders. Digits without sufficient untouched evidence remain **DO NOT TRADE**.


## Railway cloud deployment

The supplied `Dockerfile` is intended for Railway. It listens on Railway's assigned port, stores SQLite data at `/data/bromax.sqlite3`, and refuses to start without authentication credentials. Do not deploy the app on a public URL without configuring both credentials.

1. Create a **private** GitHub repository and push this project to it. Do not commit `.env`, database files, reports, approval files, or credentials.
2. In Railway, create a project from that GitHub repository and deploy the service using the included `Dockerfile`.
3. Add a Railway volume to the service, mounted at `/data`. This is required to preserve tick history and research snapshots across redeployments.
4. In the service's Variables, set `BROMAX_AUTH_USERNAME` and `BROMAX_AUTH_PASSWORD` to a username and a unique, strong password. Generate and store the password in a password manager; do not put it in source control.
5. Wait for deployment, then generate a Railway public domain in the service's Networking settings. Open the HTTPS dashboard URL from a phone or another computer and enter the configured Basic Auth credentials.

All dashboard and API endpoints, including API documentation, require those credentials. Use the generated HTTPS domain rather than plain HTTP. Railway hosting and persistent-volume charges may apply. The service remains paper-only and does not add live order placement.
