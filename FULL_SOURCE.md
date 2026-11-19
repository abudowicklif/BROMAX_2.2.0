# BROMAX 2.2.0 - Complete Source Listing

Generated from the packaged BROMAX source tree. Local credentials, databases, reports, and virtual environments are excluded.

---

## `.dockerignore`

```text
.venv
__pycache__
.pytest_cache
*.py[cod]
*.egg-info
.env
.git
data
reports
approved_strategies.json
```

## `.env.example`

```text
DERIV_SYMBOLS=1HZ100V
DERIV_WS_URL=wss://api.derivws.com/trading/v1/options/ws/public
DATABASE_PATH=data/bromax.sqlite3
CURRENCY=USD
PAPER_STAKE=1.00
MAX_PAPER_STAKE=1.00
DAILY_LOSS_CAP=10.00
MAX_TRADES_PER_DAY=100
MAX_DATA_AGE_SECONDS=5
MIN_HISTORY_TICKS=1000
PROPOSAL_TIMEOUT_SECONDS=4
MAX_PENDING_QUOTE_TASKS=20
```

## `.gitignore`

```text
.venv/
__pycache__/
.pytest_cache/
*.py[cod]
.env
data/*.sqlite3
data/*.sqlite3-shm
data/*.sqlite3-wal
reports/*.json
approved_strategies.json
*.egg-info/
data/*
!data/.gitkeep
reports/*
!reports/.gitkeep
```

## `app.py`

```python
"""ASGI entry point: run with `uvicorn app:app`."""
from bromax.api import app

__all__ = ["app"]
```

## `bromax/__init__.py`

```python
"""BROMAX: auditable MATCH-only research and paper simulation."""
__version__ = "1.0.0"
```

## `bromax/agents.py`

```python
"""Deterministic specialist agents and the final paper-signal coordinator.

All probabilities, validation evidence, payout values, and approvals are
produced by the existing Python research and risk functions. No LLM output can
change a numerical result or authorize a signal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import logging
import math
import time
from typing import Any, Callable, Literal

from .digits import extract_last_digit

AgentStatus = Literal["APPROVED", "REJECTED", "INSUFFICIENT_EVIDENCE", "MONITORING", "ERROR"]
log = logging.getLogger("bromax.agents")


@dataclass(frozen=True)
class AgentResult:
    agent_id: str
    status: AgentStatus
    evaluated_at: str
    duration_ms: float
    summary: str
    reasons: tuple[str, ...]
    metrics: dict[str, Any]
    output: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _result(agent_id: str, status: AgentStatus, summary: str, reasons=(), metrics=None, output=None):
    return AgentResult(
        agent_id=agent_id,
        status=status,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=0.0,
        summary=summary,
        reasons=tuple(dict.fromkeys(str(reason) for reason in reasons if reason)),
        metrics=metrics or {},
        output=output or {},
    )


def _run(agent_id: str, evaluate: Callable[[], AgentResult]) -> AgentResult:
    started = time.perf_counter()
    try:
        result = evaluate()
    except (KeyError, TypeError, ValueError, ArithmeticError, OverflowError) as error:
        log.exception("Agent %s rejected malformed analysis input", agent_id)
        result = _result(agent_id, "ERROR", "The agent could not validate its analysis input.", (str(error),))
    result = replace(result, duration_ms=round((time.perf_counter() - started) * 1000, 3))
    log.info("Agent %s status=%s duration_ms=%.3f", agent_id, result.status, result.duration_ms)
    return result


class DataIntelligenceAgent:
    """Input: recent stored ticks and collector freshness. Output: data-quality gate."""

    agent_id = "data_intelligence"

    def run(self, ticks: list[dict[str, Any]], feed_stale: bool, max_age_seconds: int, errors=()):
        def evaluate():
            if len(ticks) < 2:
                return _result(
                    self.agent_id, "INSUFFICIENT_EVIDENCE", "At least two stored ticks are required.",
                    ("Tick history is too short to verify sequence continuity.",),
                    {"ticks_checked": len(ticks), "quality_score": None},
                )
            reasons = []
            epochs = [int(row["epoch"]) for row in ticks]
            duplicate_free = len(epochs) == len(set(epochs))
            monotonic = all(right > left for left, right in zip(epochs, epochs[1:]))
            precision_valid = True
            malformed = 0
            for row in ticks:
                try:
                    _, digit = extract_last_digit(row["quote_text"], row["pip_size"])
                    if digit != row["digit"]:
                        precision_valid = False
                        malformed += 1
                except (KeyError, TypeError, ValueError):
                    precision_valid = False
                    malformed += 1
            recent = ticks[-21:]
            gaps = [right - left - 1 for left, right in zip(
                [int(row["epoch"]) for row in recent],
                [int(row["epoch"]) for row in recent[1:]],
            ) if right - left > 1]
            recent_contiguous = not gaps and all(
                right - left == 1 for left, right in zip(
                    [int(row["epoch"]) for row in recent],
                    [int(row["epoch"]) for row in recent[1:]],
                )
            )
            latest_received = datetime.fromisoformat(str(ticks[-1]["received_utc"]))
            if latest_received.tzinfo is None:
                latest_received = latest_received.replace(tzinfo=timezone.utc)
            raw_age_seconds = (datetime.now(timezone.utc) - latest_received).total_seconds()
            age_seconds = max(0.0, raw_age_seconds)
            fresh = not feed_stale and raw_age_seconds >= -1 and age_seconds <= max_age_seconds
            checks = {
                "fresh": fresh,
                "timestamp_not_future": raw_age_seconds >= -1,
                "unique_epochs": duplicate_free,
                "monotonic_epochs": monotonic,
                "displayed_digits_valid": precision_valid,
                "recent_ticks_contiguous": recent_contiguous,
            }
            if not fresh:
                reasons.append("Market data is stale or exceeds the configured freshness limit.")
            if not duplicate_free:
                reasons.append("Duplicate tick epochs were detected.")
            if not monotonic:
                reasons.append("Tick timestamps are not strictly increasing.")
            if not precision_valid:
                reasons.append("Stored quotes, pip precision, and extracted digits do not agree.")
            if not recent_contiguous:
                reasons.append("A missing tick was detected in the recent sequence.")
            status: AgentStatus = "APPROVED" if all(checks.values()) else "REJECTED"
            score = round(100 * sum(checks.values()) / len(checks))
            return _result(
                self.agent_id, status,
                "Recent ticks passed all data-quality checks." if status == "APPROVED" else "Data quality blocks new signals.",
                reasons,
                {
                    "quality_score": score,
                    "ticks_checked": len(ticks),
                    "latest_tick_age_seconds": round(age_seconds, 3),
                    "missing_ticks_recent": sum(gaps),
                    "malformed_ticks": malformed,
                    "checks": checks,
                    "recent_collector_errors": list(errors)[-5:],
                },
            )
        return _run(self.agent_id, evaluate)


class StrategyResearchAgent:
    """Input: frozen per-symbol report. Output: validation/final-test disposition."""

    agent_id = "strategy_research"

    def run(self, report_item: dict[str, Any]):
        def evaluate():
            evidence = report_item.get("digit_evidence") or {}
            selected = evidence.get("selected_strategy_id")
            finalists = report_item.get("validation_finalists") or []
            strategies = report_item.get("strategies") or []
            result = next((row for row in strategies if row.get("strategy_id") == selected), None)
            if not selected or not result:
                return _result(
                    self.agent_id, "INSUFFICIENT_EVIDENCE", "No development-selected strategy has report evidence.",
                    ("A selected strategy and its frozen research result are unavailable.",),
                )
            validation = result.get("validation") or {}
            final_test = result.get("final_test") or {}
            verdict = result.get("final_test_verdict") or result.get("final_test_status")
            finalist = selected in finalists
            reasons = []
            if not finalist:
                reasons.append("The selected strategy did not pass the pre-registered validation gate.")
            if not final_test:
                reasons.append("The untouched final test is unavailable for the selected strategy.")
            elif verdict != "evidence_of_edge":
                reasons.append("The selected strategy lacks positive untouched final-test evidence.")
            passed = finalist and bool(final_test) and verdict == "evidence_of_edge"
            return _result(
                self.agent_id, "APPROVED" if passed else "REJECTED" if final_test else "INSUFFICIENT_EVIDENCE",
                "The strategy passed validation and untouched-test gates." if passed else "The strategy is not validated for a paper signal.",
                reasons,
                {
                    "selected_strategy_id": selected,
                    "validation_finalist": finalist,
                    "validation_n": validation.get("n"),
                    "validation_quote_coverage": (validation.get("payout_adjusted") or {}).get("quote_coverage"),
                    "final_test_verdict": verdict,
                    "final_test_n": final_test.get("n"),
                },
            )
        return _run(self.agent_id, evaluate)


class ProbabilityCalibrationAgent:
    """Input: per-digit evidence and candidate probability vectors. Output: numeric integrity."""

    agent_id = "probability_calibration"

    def run(self, digit_analysis: dict[str, Any], signals: dict[str, Any]):
        def evaluate():
            rows = digit_analysis.get("digits") or []
            digits = sorted(int(row["digit"]) for row in rows)
            if digits != list(range(10)):
                return _result(
                    self.agent_id, "REJECTED", "The analysis does not contain exactly one row for every digit.",
                    ("Digit coverage must be exactly 0 through 9.",),
                    {"digit_rows": len(rows)},
                )
            reasons = []
            estimates = 0
            health_scores = []
            calibration_errors = []
            for row in rows:
                probability = row.get("estimated_win_probability")
                if probability is not None:
                    probability = float(probability)
                    if not math.isfinite(probability) or not 0 <= probability <= 1:
                        reasons.append(f"Digit {row['digit']} has an invalid probability.")
                    else:
                        estimates += 1
                health = row.get("health_percent")
                if health is not None:
                    health = float(health)
                    if not math.isfinite(health) or not 0 <= health <= 100:
                        reasons.append(f"Digit {row['digit']} has an invalid evidence-health score.")
                    else:
                        health_scores.append(health)
                calibration = (row.get("validation") or {}).get("absolute_calibration_error")
                if calibration is not None:
                    calibration_errors.append(float(calibration))
            vectors = signals.get("signals") or []
            disagreement = set()
            for signal in vectors:
                probabilities = [float(value) for value in signal.get("probabilities") or []]
                if len(probabilities) != 10 or any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities):
                    reasons.append(f"Strategy {signal.get('strategy_id', 'unknown')} has an invalid ten-digit probability vector.")
                    continue
                if not math.isclose(sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-6):
                    reasons.append(f"Strategy {signal.get('strategy_id', 'unknown')} probabilities do not sum to one.")
                    continue
                predicted_digit=int(signal["predicted_digit"])
                if predicted_digit not in range(10):
                    reasons.append(f"Strategy {signal.get('strategy_id', 'unknown')} predicted an invalid digit.")
                    continue
                disagreement.add(predicted_digit)
            if reasons:
                return _result(
                    self.agent_id, "REJECTED", "Probability output failed numerical validation.", reasons,
                    {"digit_rows": len(rows), "valid_digit_estimates": estimates},
                )
            if not vectors or estimates != 10:
                return _result(
                    self.agent_id, "INSUFFICIENT_EVIDENCE", "A current probability estimate is unavailable for every digit.",
                    ("Probability output remains blank where the selected model has no valid estimate.",),
                    {"digit_rows": len(rows), "valid_digit_estimates": estimates, "candidate_vectors": len(vectors)},
                )
            return _result(
                self.agent_id, "APPROVED", "Ten finite digit probabilities and candidate vectors passed integrity checks.",
                metrics={
                    "valid_digit_estimates": estimates,
                    "candidate_vectors": len(vectors),
                    "model_disagreement_count": len(disagreement),
                    "mean_evidence_health": round(sum(health_scores) / len(health_scores), 3) if health_scores else None,
                    "mean_validation_calibration_error": round(sum(calibration_errors) / len(calibration_errors), 6) if calibration_errors else None,
                },
                output={"candidate_digits": sorted(disagreement)},
            )
        return _run(self.agent_id, evaluate)


class MarketRegimeAgent:
    """Input: timing diagnostics and current volatility filter. Output: regime compatibility."""

    agent_id = "market_regime"

    def run(self, timing: dict[str, Any], digit_analysis: dict[str, Any]):
        def evaluate():
            filter_data = digit_analysis.get("volatility_filter") or {}
            status = filter_data.get("status")
            if status == "PASS":
                agent_status: AgentStatus = "APPROVED"
                reasons = ()
            elif status == "FAIL":
                agent_status = "REJECTED"
                reasons = ("The selected strategy is not statistically confirmed in the current volatility regime.",)
            else:
                agent_status = "INSUFFICIENT_EVIDENCE"
                reasons = ("Current-regime validation evidence is unavailable.",)
            vol = digit_analysis.get("volatility") or {}
            return _result(
                self.agent_id, agent_status,
                "Current regime passed its strategy-specific holdout filter." if agent_status == "APPROVED" else "Current regime does not approve a signal.",
                reasons,
                {
                    "current_regime": filter_data.get("regime"),
                    "volatility_health_percent": filter_data.get("health_percent"),
                    "checks_passed": filter_data.get("checks_passed"),
                    "checks_total": filter_data.get("checks_total"),
                    "short_term_volatility": vol.get("short_term"),
                    "long_term_volatility": vol.get("long_term"),
                    "strategies_evaluated": len(timing.get("strategies") or []),
                },
            )
        return _run(self.agent_id, evaluate)


class ContractIntelligenceAgent:
    """Input: approved-ready candidate and its digit/duration quote. Output: EV verification."""

    agent_id = "contract_intelligence"

    def run(self, ready: dict[str, Any], digit_analysis: dict[str, Any]):
        def evaluate():
            signal = ready.get("signal") if ready.get("eligible") is True else None
            if not signal:
                status = "REJECTED" if ready.get("status") in {"REJECTED", "EXPIRED"} else "INSUFFICIENT_EVIDENCE"
                return _result(
                    self.agent_id, status, "No current fully-qualified contract quote is available.",
                    (ready.get("reason") or "No eligible quote-backed signal exists.",),
                )
            digit = int(signal["predicted_digit"])
            duration = int(signal["duration"])
            row = next((item for item in digit_analysis.get("digits",[]) if int(item["digit"]) == digit), None)
            duration_result = (row or {}).get("durations", {}).get(str(duration), {})
            quote = duration_result.get("current_quote") or {}
            reasons = []
            try:
                probability = Decimal(str(signal["probability"]))
                stake = Decimal(str(signal["stake"]))
                payout = Decimal(str(signal["payout"]))
                expected_value = probability * payout - stake
                published_ev = Decimal(str(signal["expected_value"]))
                spot_time = int(quote["spot_time"])
                forecast_epoch = int(signal["forecast_epoch"])
                if probability < 0 or probability > 1 or stake <= 0 or payout <= 0:
                    reasons.append("Probability, stake, or payout is outside its valid range.")
                if expected_value <= 0:
                    reasons.append("The quoted expected value is not positive.")
                if expected_value != published_ev:
                    reasons.append("The published expected value does not match the quoted contract inputs.")
                if not quote.get("valid") or spot_time != forecast_epoch:
                    reasons.append("The proposal is unavailable, expired, or mismatched to the forecast tick.")
                if not duration_result or duration_result.get("decision") != "PAPER TRADE CANDIDATE":
                    reasons.append("Per-digit duration evidence did not approve this exact contract.")
                if signal.get("duration_profitability_status") != "OUT_OF_SAMPLE_PROFIT_EVIDENCE":
                    reasons.append("The duration lacks quote-backed out-of-sample profitability evidence.")
            except (KeyError, TypeError, ValueError, ArithmeticError) as error:
                reasons.append(f"Required quote fields failed validation: {error}")
                expected_value = None
                probability = stake = payout = None
            status: AgentStatus = "APPROVED" if not reasons else "REJECTED"
            return _result(
                self.agent_id, status,
                "The fresh quote and expected value match the validated contract." if status == "APPROVED" else "Contract quote verification failed.",
                reasons,
                {
                    "digit": digit,
                    "duration_ticks": duration,
                    "probability": str(probability) if probability is not None else None,
                    "stake": str(stake) if stake is not None else None,
                    "gross_payout": str(payout) if payout is not None else None,
                    "expected_value": str(expected_value) if expected_value is not None else None,
                    "quote_spot_time": quote.get("spot_time"),
                    "forecast_epoch": signal.get("forecast_epoch"),
                },
            )
        return _run(self.agent_id, evaluate)


class RiskSignalApprovalAgent:
    """Input: specialist results and READY response. Output: independent deterministic veto."""

    agent_id = "risk_signal_approval"

    def run(self, results: list[AgentResult], ready: dict[str, Any], digit_analysis: dict[str, Any], consistent: bool):
        def evaluate():
            by_id = {result.agent_id: result for result in results}
            signal = ready.get("signal") if ready.get("eligible") is True else None
            checks = {
                "snapshot_consistent": consistent,
                "data_quality": by_id.get("data_intelligence") is not None and by_id["data_intelligence"].status == "APPROVED",
                "strategy_validation": by_id.get("strategy_research") is not None and by_id["strategy_research"].status == "APPROVED",
                "probability_integrity": by_id.get("probability_calibration") is not None and by_id["probability_calibration"].status == "APPROVED",
                "market_regime": by_id.get("market_regime") is not None and by_id["market_regime"].status == "APPROVED",
                "contract_validation": by_id.get("contract_intelligence") is not None and by_id["contract_intelligence"].status == "APPROVED",
                "ready_gate": signal is not None,
            }
            reasons = []
            if not consistent:
                reasons.append("The market tick changed during analysis; discard this snapshot.")
            for key, passed in checks.items():
                if not passed and key != "snapshot_consistent":
                    agent = {
                        "data_quality":"data_intelligence",
                        "strategy_validation":"strategy_research",
                        "probability_integrity":"probability_calibration",
                        "market_regime":"market_regime",
                        "contract_validation":"contract_intelligence",
                    }.get(key)
                    reason = next(iter(by_id[agent].reasons),None) if agent and agent in by_id else None
                    reasons.append(reason or f"Required check failed: {key.replace('_',' ')}.")
            if signal and signal.get("strategy_id") not in (ready.get("approved_strategy_ids") or []):
                reasons.append("The strategy does not have explicit user approval.")
                checks["explicit_user_approval"] = False
            elif signal:
                checks["explicit_user_approval"] = True
            if signal and signal.get("execution_mode") != "PAPER_SIMULATION_ONLY":
                reasons.append("The candidate is not marked as paper-simulation-only.")
                checks["paper_only_mode"] = False
            elif signal:
                checks["paper_only_mode"] = True
            digit = int(signal["predicted_digit"]) if signal else None
            digit_row = next((row for row in digit_analysis.get("digits",[]) if int(row["digit"]) == digit),None) if digit is not None else None
            if signal and (not digit_row or digit_row.get("decision") != "PAPER TRADE CANDIDATE"):
                reasons.append("Per-digit untouched evidence did not approve the selected digit.")
                checks["per_digit_evidence"] = False
            elif signal:
                checks["per_digit_evidence"] = True
            failed = any(not passed for passed in checks.values())
            if not failed:
                status: AgentStatus = "APPROVED"
                summary = "All independent paper-signal safety checks passed."
            elif not consistent or any(
                by_id.get(agent) and by_id[agent].status == "INSUFFICIENT_EVIDENCE"
                for agent in ("data_intelligence","strategy_research","probability_calibration","market_regime","contract_intelligence")
            ):
                status = "INSUFFICIENT_EVIDENCE"
                summary = "No trade: required evidence is incomplete."
            else:
                status = "REJECTED"
                summary = "Signal rejected by an independent risk check."
            return _result(
                self.agent_id, status, summary, reasons,
                {"checks":checks,"checks_passed":sum(checks.values()),"checks_total":len(checks)},
                {"signal":signal if status=="APPROVED" else None,"trade_authorized":status=="APPROVED"},
            )
        return _run(self.agent_id, evaluate)


class PerformanceLearningAgent:
    """Input: immutable paper/forecast ledgers and prior agent runs. Output: monitoring metrics."""

    agent_id = "performance_learning"

    def run(self, paper_events: list[dict[str, Any]], forecasts: list[dict[str, Any]], prior_runs: list[dict[str, Any]]):
        def evaluate():
            opened = [event for event in paper_events if event.get("event_type") == "PAPER_OPEN"]
            settled = [event for event in paper_events if event.get("event_type") == "PAPER_SETTLE"]
            wins = sum(event.get("hit") == 1 for event in settled)
            pnls = [float(event["paper_pnl"]) for event in settled if event.get("paper_pnl") is not None]
            scored = [row for row in forecasts if row.get("actual_digit") is not None and row.get("probabilities")]
            brier_values = []
            calibration_values = []
            for row in scored:
                probabilities = [float(value) for value in row["probabilities"]]
                actual = int(row["actual_digit"])
                prediction = int(row["prediction"])
                if (
                    len(probabilities) != 10
                    or any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities)
                    or not math.isclose(sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-6)
                    or actual not in range(10)
                    or prediction not in range(10)
                ):
                    continue
                brier_values.append(sum((probabilities[digit] - int(digit == actual)) ** 2 for digit in range(10)))
                calibration_values.append((probabilities[prediction], int(prediction == actual)))
            brier = sum(brier_values) / len(brier_values) if brier_values else None
            calibration_error = (
                abs(sum(prob for prob, _ in calibration_values) / len(calibration_values)
                    - sum(hit for _, hit in calibration_values) / len(calibration_values))
                if calibration_values else None
            )
            statuses: dict[str, dict[str, Any]] = {}
            for run in prior_runs:
                for item in run.get("agent_results",[]):
                    agent_id=item.get("agent_id")
                    if not agent_id:
                        continue
                    summary=statuses.setdefault(agent_id,{"runs":0,"errors":0,"last_status":None})
                    summary["runs"]+=1
                    summary["errors"]+=int(item.get("status")=="ERROR")
                    if summary["last_status"] is None:
                        summary["last_status"]=item.get("status")
            for summary in statuses.values():
                summary["error_rate"]=summary["errors"]/summary["runs"] if summary["runs"] else None
            has_results=bool(settled or brier_values)
            return _result(
                self.agent_id, "MONITORING" if has_results or prior_runs else "INSUFFICIENT_EVIDENCE",
                "Performance monitoring uses recorded outcomes only." if has_results else "No settled outcomes are available yet.",
                () if has_results else ("Paper and scored forecast histories are empty.",),
                {
                    "paper_opened":len(opened),
                    "paper_settled":len(settled),
                    "wins":wins,
                    "losses":len(settled)-wins,
                    "win_rate":wins/len(settled) if settled else None,
                    "net_paper_profit":sum(pnls) if pnls else None,
                    "scored_forecasts":len(brier_values),
                    "recent_brier_multiclass":brier,
                    "recent_absolute_calibration_error":calibration_error,
                    "monitored_agents":statuses,
                },
            )
        return _run(self.agent_id, evaluate)


class ChiefIntelligenceCoordinator:
    """Input: all seven specialist results. Output: one coherent paper-only decision."""

    agent_id = "chief_intelligence_coordinator"

    def coordinate(self, specialist_results: list[AgentResult], risk_result: AgentResult, consistent: bool):
        def evaluate():
            if not consistent:
                final_status = "NO TRADE — INSUFFICIENT EVIDENCE"
            elif risk_result.status == "APPROVED":
                final_status = "APPROVED — PAPER SIGNAL"
            elif risk_result.status == "REJECTED":
                final_status = "REJECTED — FAILED VALIDATION"
            else:
                final_status = "NO TRADE — INSUFFICIENT EVIDENCE"
            errors=[result.agent_id for result in specialist_results if result.status=="ERROR"]
            if errors:
                final_status="NO TRADE — INSUFFICIENT EVIDENCE"
            return _result(
                self.agent_id,
                "APPROVED" if final_status=="APPROVED — PAPER SIGNAL" else
                "REJECTED" if final_status=="REJECTED — FAILED VALIDATION" else "INSUFFICIENT_EVIDENCE",
                final_status,
                risk_result.reasons,
                {
                    "specialists_completed":len(specialist_results),
                    "specialist_errors":errors,
                    "snapshot_consistent":consistent,
                    "optional_llm_research":"DISABLED_NOT_REQUIRED",
                },
                {"final_status":final_status,"trade_authorized":final_status=="APPROVED — PAPER SIGNAL"},
            )
        return _run(self.agent_id, evaluate)


def coordinate_snapshot(
    *,
    report_item: dict[str, Any],
    digit_analysis: dict[str, Any],
    signal_data: dict[str, Any],
    timing: dict[str, Any],
    ready: dict[str, Any],
    quality_ticks: list[dict[str, Any]],
    feed_stale: bool,
    max_data_age_seconds: int,
    collector_errors: list[str],
    paper_events: list[dict[str, Any]],
    forecasts: list[dict[str, Any]],
    prior_runs: list[dict[str, Any]],
    consistent: bool,
    snapshot_timestamp: str | None = None,
) -> dict[str, Any]:
    """Run the eight deterministic roles over explicit, role-scoped inputs."""
    data_result=DataIntelligenceAgent().run(quality_ticks,feed_stale,max_data_age_seconds,collector_errors)
    strategy_result=StrategyResearchAgent().run(report_item)
    probability_result=ProbabilityCalibrationAgent().run(digit_analysis,signal_data)
    regime_result=MarketRegimeAgent().run(timing,digit_analysis)
    contract_result=ContractIntelligenceAgent().run(ready,digit_analysis)
    first_five=[data_result,strategy_result,probability_result,regime_result,contract_result]
    risk_result=RiskSignalApprovalAgent().run(first_five,ready,digit_analysis,consistent)
    performance_result=PerformanceLearningAgent().run(paper_events,forecasts,prior_runs)
    specialist_results=first_five+[risk_result,performance_result]
    coordinator_result=ChiefIntelligenceCoordinator().coordinate(specialist_results,risk_result,consistent)
    results=specialist_results+[coordinator_result]
    timestamp=snapshot_timestamp or datetime.now(timezone.utc).isoformat()
    results=[replace(result,evaluated_at=timestamp) for result in results]
    coordinator_result=results[-1]
    decision=coordinator_result.output
    return {
        "decision":decision,
        "agents":[result.as_dict() for result in results],
        "performance":performance_result.metrics,
    }
```

## `bromax/api.py`

```python
import asyncio, base64, binascii, hmac, logging, os, time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pathlib import Path
import json
from threading import Lock
from decimal import Decimal, InvalidOperation
from .config import Settings
from .storage import Store
from .collector import MarketCollector
from .paperbot import ExecutionInterface, PaperBot
from .agents import coordinate_snapshot
from .reporting import write_report
from .intelligence import analyze_ticks
from .timing import evaluate_timing, evaluate_payout_timing
from .strategies import candidate_strategies, ForecastContext
from uuid import uuid4

logging.basicConfig(level=logging.INFO)
settings=Settings.from_env(); store=Store(settings.database_path)
paperbot=PaperBot(store,settings.paper_stake,settings.max_paper_stake,settings.daily_loss_cap,settings.max_trades_per_day)
execution_interface=ExecutionInterface()
collector=MarketCollector(settings,store,paperbot)
collector_task=None
_report_cache={"created":0.0,"value":None}
_timing_cache={}
_report_lock=Lock()
_timing_lock=Lock()

def _auth_credentials():
    username=os.getenv("BROMAX_AUTH_USERNAME")
    password=os.getenv("BROMAX_AUTH_PASSWORD")
    if bool(username) != bool(password):
        raise RuntimeError("Set both BROMAX_AUTH_USERNAME and BROMAX_AUTH_PASSWORD")
    return username,password

@asynccontextmanager
async def lifespan(app: FastAPI):
    global collector_task
    auth_required=os.getenv("BROMAX_REQUIRE_AUTH","").lower() in {"1","true","yes"}
    username,password=_auth_credentials()
    if auth_required and not username:
        raise RuntimeError("BROMAX_REQUIRE_AUTH is enabled but authentication credentials are not configured")
    collector_task=asyncio.create_task(collector.run_forever())
    yield
    collector_task.cancel()
    try: await collector_task
    except asyncio.CancelledError: pass

app=FastAPI(title="BROMAX MATCH Research",version="2.2.0",lifespan=lifespan)

@app.middleware("http")
async def require_basic_auth(request, call_next):
    username,password=_auth_credentials()
    auth_required=os.getenv("BROMAX_REQUIRE_AUTH","").lower() in {"1","true","yes"}
    if not username:
        if auth_required:
            raise RuntimeError("BROMAX_REQUIRE_AUTH is enabled but authentication credentials are not configured")
        return await call_next(request)
    header=request.headers.get("authorization","")
    scheme,separator,encoded=header.partition(" ")
    if separator and scheme.lower()=="basic":
        try:
            decoded=base64.b64decode(encoded,validate=True).decode("utf-8")
        except (binascii.Error,UnicodeDecodeError,ValueError):
            decoded=""
        supplied_username,colon,supplied_password=decoded.partition(":")
        if (colon and hmac.compare_digest(supplied_username.encode("utf-8"),username.encode("utf-8"))
                and hmac.compare_digest(supplied_password.encode("utf-8"),password.encode("utf-8"))):
            return await call_next(request)
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="BROMAX", charset="UTF-8"'},
    )

@app.get("/health")
def health():
    with store.connect() as c:
        counts={s:c.execute("SELECT COUNT(*) FROM ticks WHERE symbol=?",(s,)).fetchone()[0] for s in settings.symbols}
        latest={}
        for s in settings.symbols:
            r=c.execute("SELECT epoch,quote_text,received_utc FROM ticks WHERE symbol=? ORDER BY epoch DESC,id DESC LIMIT 1",(s,)).fetchone()
            latest[s]=dict(r) if r else None
    stale={s:(collector.last_received.get(s) is None or time.time()-collector.last_received[s]>settings.max_data_age_seconds) for s in settings.symbols}
    pipeline={s:_pipeline_state(s) for s in settings.symbols}
    return {"status":"degraded" if any(stale.values()) else "ok","mode":"paper_only","trading_enabled":False,"symbols":settings.symbols,"tick_counts":counts,"latest":latest,"stale":stale,"pipeline":pipeline,"recent_errors":collector.errors[-10:]}

def _pipeline_state(symbol):
    state=collector.operational_status(symbol)
    state["recent_errors"]=collector.errors[-5:]
    with store.connect() as c:
        state["stored_ticks"]=c.execute("SELECT COUNT(*) FROM ticks WHERE symbol=?",(symbol,)).fetchone()[0]
        state["stored_forecasts"]=c.execute(
            "SELECT COUNT(*) FROM audit_events WHERE symbol=? AND event_type='FORECAST'",(symbol,)
        ).fetchone()[0]
    return state

@app.post("/safety/kill-switch")
def engage_execution_kill_switch():
    """Engage the one-way live-execution kill switch; never enables orders."""
    execution_interface.engage_kill_switch()
    logging.getLogger("bromax.safety").warning("Execution kill switch engaged")
    return {
        "status":"KILL_SWITCH_ENGAGED",
        "kill_switch_engaged":execution_interface.kill_switch_engaged,
        "live_execution_implemented":False,
        "live_execution_enabled":False,
    }

@app.get("/ticks/latest")
def latest_ticks(symbol: str, limit: int=50):
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    with store.connect() as c:
        rows=c.execute("SELECT id,epoch,quote_text,pip_size,digit,received_utc FROM ticks WHERE symbol=? ORDER BY epoch DESC,id DESC LIMIT ?",(symbol,max(1,min(limit,1000)))).fetchall()
    return [dict(r) for r in rows]

@app.get("/digits/distribution")
def distribution(symbol: str, window: int=1000):
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    rows=store.history(symbol,max(10,min(window,10000))); counts={str(i):0 for i in range(10)}
    for r in rows: counts[str(r["digit"])] += 1
    n=len(rows)
    return {"symbol":symbol,"n":n,"counts":counts,"frequencies":{k:(v/n if n else None) for k,v in counts.items()},"uniform_reference":0.1}

@app.get("/forecasts")
def forecasts(symbol: str, limit: int = 500):
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    if limit < 1 or limit > 5000: raise HTTPException(422,"limit must be between 1 and 5000")
    return store.forecasts(symbol,limit=limit)

@app.get("/signals/history")
def signal_history(symbol: str, limit: int = 50):
    if symbol not in settings.symbols:
        raise HTTPException(404,"Symbol not configured")
    if limit < 1 or limit > 500:
        raise HTTPException(422,"limit must be between 1 and 500")
    rows=store.forecasts(symbol,limit=limit)
    latest=store.quality_history(symbol,1)
    latest_epoch=int(latest[-1]["epoch"]) if latest else None
    status=_pipeline_state(symbol)
    now=time.time()
    current_feed=(
        collector.last_received.get(symbol) is not None
        and now-collector.last_received[symbol]<=settings.max_data_age_seconds
    )
    signals=[]
    for row in rows:
        actual=row.get("actual_digit")
        if actual is not None:
            signal_status="SCORED_HIT" if row.get("hit") else "SCORED_MISS"
        elif current_feed and int(row["forecast_epoch"])==latest_epoch:
            signal_status="CURRENT"
        else:
            signal_status="EXPIRED_UNSCORED"
        probabilities=row.get("probabilities") or []
        prediction=int(row["prediction"])
        confidence=probabilities[prediction] if prediction<len(probabilities) else None
        signals.append({
            "symbol":symbol,
            "strategy_id":row["strategy_id"],
            "prediction":prediction,
            "confidence":confidence,
            "forecast_epoch":int(row["forecast_epoch"]),
            "target_epoch":int(row["forecast_epoch"])+1,
            "created_at":row["created_utc"],
            "status":signal_status,
            "actual_digit":actual,
            "hit":row.get("hit"),
        })
    return {"symbol":symbol,"pipeline":status,"signals":signals,"trade_authorized":False,
            "note":"Persisted strategy forecasts are analysis records, not trade instructions. CURRENT applies only to the freshest tick while the live feed is fresh."}

@app.get("/report")
def report():
    with _report_lock:
        if _report_cache["value"] is None or time.monotonic()-_report_cache["created"]>=15:
            _report_cache["value"]=write_report(store,settings.symbols)
            _report_cache["created"]=time.monotonic()
        return _report_cache["value"]

@app.get("/bridge/status")
def bridge_status():
    """Health of the intelligence-to-paper-execution handoff; never places live orders."""
    report_path=Path("reports/bromax_report.json")
    approvals_path=Path("approved_strategies.json")
    approvals={}
    if approvals_path.exists():
        try: approvals=json.loads(approvals_path.read_text()).get("approved",{})
        except Exception: approvals={}
    latest={}
    for symbol in settings.symbols:
        rows=store.forecasts(symbol,limit=1)
        row=rows[-1] if rows else None
        latest[symbol]={
            "forecast": row,
            "handoff": "paper_candidate_only" if row else "waiting_for_forecast",
            "approved_strategy_ids": approvals.get(symbol,[]),
        }
    payout_choices={symbol:evaluate_payout_timing(store.duration_events(symbol)) for symbol in settings.symbols}
    return {
        "system": "BROMAX Intelligence + Deriv Paper Execution Bridge",
        "mode": "SIMULATED_ONLY",
        "market_data": "public_deriv_ticks",
        "quote_source": "Deriv_read_only_proposals",
        "execution": "paper_only_no_buy_endpoint",
        "strategy_approval_file_present": approvals_path.exists(),
        "research_report_present": report_path.exists(),
        "symbols": latest,
        "contract_protocol": {"type": "DIGITMATCH", "duration_candidates": [1,2,3,4,5], "duration_unit": "t", "selection": "quote_backed_out_of_sample_profitability"},
        "payout_optimized_duration": {s:r.get("best_duration") for s,r in payout_choices.items()},
        "note": "The intelligence engine freezes forecasts, captures read-only Deriv proposals for 1-5 ticks, and settles duration research contracts against exact future ticks. The READY panel exposes only the statistically selected duration after quote, volatility and risk checks. No real account authentication or live order placement exists."
    }


@app.get("/signals/ready")
def ready_signal(symbol: str):
    """Expose the statistically selected duration when its live quote is still valid.

    The collector has already recorded every candidate duration in the paper
    quote/settlement ledger. This endpoint never buys or sends a live order.
    """
    from decimal import Decimal, InvalidOperation
    import time as _time
    if symbol not in settings.symbols:
        raise HTTPException(404, "Symbol not configured")
    rows=store.forecasts(symbol)
    approved_ids=[]
    try:
        approval_data=json.loads(Path("approved_strategies.json").read_text())
        approved_ids=approval_data.get("approved",{}).get(symbol,[])
    except Exception: pass
    if not rows:
        return {"symbol":symbol,"status":"WAITING","eligible":False,"reason":"No forecast has been recorded for this market yet.","signal":None,"approved_strategy_ids":approved_ids}
    payout_evidence=evaluate_payout_timing(store.duration_events(symbol))
    best=payout_evidence.get("best_candidate")
    if not best:
        return {"symbol":symbol,"status":"WAITING","eligible":False,
                "reason":"No duration has passed quote-backed, chronological, payout-adjusted out-of-sample profitability testing. Candidate forecasts remain research-only.",
                "signal":None,"approved_strategy_ids":approved_ids,
                "payout_timing":{"status":payout_evidence.get("status"),"best_duration":None}}
    strategy_id=best.get("strategy_id"); duration=int(best.get("duration_ticks") or 0)
    if strategy_id not in approved_ids or not paperbot.approved(symbol,strategy_id):
        return {"symbol":symbol,"status":"WAITING","eligible":False,
                "reason":"The payout-optimized strategy does not have the separate strategy approval required for paper signals.",
                "signal":None,"approved_strategy_ids":approved_ids,
                "payout_timing":{"best_duration":duration,"strategy_id":strategy_id}}
    latest=next((row for row in reversed(rows) if row.get("strategy_id")==strategy_id),None)
    if latest is None or latest.get("hit") is not None:
        return {"symbol":symbol,"status":"WAITING","eligible":False,
                "reason":"Waiting for a fresh forecast from the payout-optimized strategy.","signal":None,
                "approved_strategy_ids":approved_ids,"payout_timing":{"best_duration":duration}}
    q=store.duration_quote(latest["seq"],duration)
    if not q:
        return {"symbol":symbol,"status":"WAITING","eligible":False,
                "reason":"The selected duration does not have a contemporaneous Deriv proposal quote for this forecast tick.",
                "signal":None,"approved_strategy_ids":approved_ids,"payout_timing":{"best_duration":duration}}
    if int(q.get("quote_spot_time") or 0)!=int(latest.get("forecast_epoch") or 0) or _time.time()>=int(latest["forecast_epoch"])+1:
        return {"symbol":symbol,"status":"EXPIRED","eligible":False,
                "reason":"The duration-specific proposal quote is stale or its one-tick entry window has expired.",
                "signal":None,"approved_strategy_ids":approved_ids}
    try:
        probability=Decimal(str((latest.get("probabilities") or [])[int(latest["prediction"])]))
        payout=Decimal(str(q["payout"])); stake=Decimal(str(q["stake"]))
        expected_value=probability*payout-stake; break_even=stake/payout
    except (InvalidOperation,ValueError,TypeError,IndexError,ZeroDivisionError):
        return {"symbol":symbol,"status":"REJECTED","eligible":False,"reason":"Forecast probabilities or duration proposal values failed validation.","signal":None,"approved_strategy_ids":approved_ids}
    if expected_value<=0:
        return {"symbol":symbol,"status":"REJECTED","eligible":False,
                "reason":"The current duration-specific proposal does not have positive model-estimated expected value.","signal":None,
                "approved_strategy_ids":approved_ids}
    timing_evidence=evaluate_timing(store.history(symbol,limit=10000),rows)
    current_regime=timing_evidence.get("current_volatility_regime")
    timing_row=next((x for x in timing_evidence.get("strategies",[]) if x.get("strategy_id")==strategy_id and x.get("duration_ticks")==duration),None)
    regime_row=next((x for x in (timing_row or {}).get("volatility_regimes",[]) if x.get("regime")==current_regime),None)
    if not regime_row or regime_row.get("status")!="CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON":
        return {"symbol":symbol,"status":"REJECTED","eligible":False,
                "reason":"Volatility confirmation is not established for the selected strategy, duration and current regime on untouched final-test data.",
                "signal":None,"approved_strategy_ids":approved_ids,
                "volatility":{"regime":current_regime,"status":"UNCONFIRMED"}}
    if not paperbot.limits_ok(symbol,stake):
        return {"symbol":symbol,"status":"REJECTED","eligible":False,
                "reason":"Paper stake, daily loss or trade-count limits block this opportunity.","signal":None,"approved_strategy_ids":approved_ids}
    signal={"signal_id":f"BROMAX-{latest['seq']}-T{duration}","symbol":symbol,
            "strategy_id":strategy_id,"predicted_digit":int(latest["prediction"]),
            "contract":"DIGITMATCH","duration":duration,"duration_unit":"t",
            "stake":str(stake),"payout":str(payout),"probability":str(probability),
            "break_even_probability":str(break_even),"expected_value":str(expected_value),
            "volatility_regime":current_regime,"volatility_confirmation":"CONFIRMED",
            "duration_profitability_status":"OUT_OF_SAMPLE_PROFIT_EVIDENCE",
            "final_test_mean_return":best["final_test"].get("mean_return_on_stake"),
            "final_test_return_ci95":best["final_test"].get("mean_return_ci95"),
            "expires_epoch":int(latest["forecast_epoch"])+1,"forecast_epoch":int(latest["forecast_epoch"]),
            "proposal_id":q.get("proposal_id"),"execution_mode":"PAPER_SIMULATION_ONLY"}
    return {"symbol":symbol,"status":"READY","eligible":True,
            "reason":"The best quote-backed duration passed final-test profitability, current quote EV, strategy approval, volatility confirmation and paper-risk checks. It is tracked in the duration paper ledger; no live order is placed.",
            "signal":signal,"approved_strategy_ids":approved_ids}


@app.get("/paper/status")
def paper_status():
    events=store.paper_events()
    opened=[e for e in events if e["event_type"]=="PAPER_OPEN"]
    settled=[e for e in events if e["event_type"]=="PAPER_SETTLE"]
    settled_ids={e["note"] for e in settled}
    pending=sum(1 for e in opened if str(e["seq"]) not in settled_ids)
    return {"enabled":True,"mode":"SIMULATED_ONLY","stake":str(settings.paper_stake),"max_stake":str(settings.max_paper_stake),"daily_loss_cap":str(settings.daily_loss_cap),"max_trades_per_day":settings.max_trades_per_day,"opened_total":len(opened),"settled_total":len(settled),"pending":pending,"approved_strategies_file":"approved_strategies.json","live_execution":False}


@app.get("/paper/trades")
def paper_trades(symbol: str | None = None):
    if symbol is not None and symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    return store.paper_events(symbol)


@app.get("/intelligence")
def intelligence(symbol: str, window: int = 1000):
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    if window < 100 or window > 10000: raise HTTPException(422,"window must be between 100 and 10000")
    frame=store.ticks_frame(symbol)
    rows=frame.tail(window).to_dict(orient="records") if not frame.empty else []
    return analyze_ticks(rows,symbol,window)

@app.get("/timing")
def timing_research(symbol: str):
    """Compare 1-5 tick horizons and volatility regimes; research only."""
    if symbol not in settings.symbols:
        raise HTTPException(404, "Symbol not configured")
    with _timing_lock:
        cached=_timing_cache.get(symbol)
        if cached and time.monotonic()-cached["created"]<5:
            return cached["value"]
        ticks = store.history(symbol, limit=10000)
        forecasts = store.forecasts(symbol, limit=50000)
        result = evaluate_timing(ticks, forecasts)
        result["payout_optimization"] = evaluate_payout_timing(store.duration_events(symbol))
        result["symbol"] = symbol
        _timing_cache[symbol]={"created":time.monotonic(),"value":result}
        return result

def _digit_duration_results(events, strategy_id):
    quotes={}
    settled={}
    for row in events:
        key=(int(row.get("forecast_seq") or 0),int(row.get("duration_ticks") or 0))
        if row.get("event_type")=="QUOTE":
            quotes[key]=row
        elif row.get("event_type")=="SETTLE":
            settled[key]=row
    result={(digit,duration):{"n":0,"wins":0,"net_pnl":0.0,"stake":0.0} for digit in range(10) for duration in (1,2,3,4,5)}
    for key,quote in quotes.items():
        outcome=settled.get(key)
        if not outcome or quote.get("strategy_id")!=strategy_id:
            continue
        try:
            epoch=int(quote["forecast_epoch"])
            duration=int(quote["duration_ticks"])
            prediction=int(quote["prediction"])
            if int(quote["quote_spot_time"])!=epoch or int(outcome["actual_epoch"])!=epoch+duration:
                continue
            stake=Decimal(str(quote["stake"])); payout=Decimal(str(quote["payout"]))
            hit=int(outcome["hit"])
        except (KeyError,TypeError,ValueError,ArithmeticError):
            continue
        if prediction not in range(10) or duration not in range(1,6) or stake<=0:
            continue
        row=result[(prediction,duration)]
        row["n"]+=1
        row["wins"]+=hit
        row["net_pnl"]+=float(payout-stake if hit else -stake)
        row["stake"]+=float(stake)
    for row in result.values():
        row["hit_rate"]=row["wins"]/row["n"] if row["n"] else None
        row["mean_return_on_stake"]=row["net_pnl"]/row["stake"] if row["stake"] else None
        row["net_pnl"]=round(row["net_pnl"],8)
    return result

@app.get("/digits/analysis")
def digit_analysis(symbol: str):
    if symbol not in settings.symbols:
        raise HTTPException(404,"Symbol not configured")
    report_data=report()
    report_item=next((x for x in report_data.get("symbols",[]) if x.get("symbol")==symbol),{})
    evidence=report_item.get("digit_evidence") or {"selected_strategy_id":None,"digits":[]}
    strategy_id=evidence.get("selected_strategy_id")
    signal_data=signals(symbol)
    current_model=next((x for x in signal_data.get("signals",[]) if x.get("strategy_id")==strategy_id),None)
    rows_by_digit={int(x["digit"]):x for x in evidence.get("digits",[])}
    history=store.history(symbol,limit=1000)
    recent=history[-100:] if history else []
    counts={digit:sum(int(row["digit"])==digit for row in history) for digit in range(10)}
    recent_counts={digit:sum(int(row["digit"])==digit for row in recent) for digit in range(10)}
    latest_quotes=store.forecasts(symbol,limit=10)
    latest_epoch=max((int(x["forecast_epoch"]) for x in latest_quotes),default=None)
    latest_quotes=[x for x in latest_quotes if latest_epoch is not None and int(x["forecast_epoch"])==latest_epoch]
    duration_events=store.duration_events(symbol)
    duration_stats=_digit_duration_results(duration_events,strategy_id)
    approved=bool(strategy_id and paperbot.approved(symbol,strategy_id))
    timing=timing_research(symbol)
    payout=timing.get("payout_optimization") or {}
    best_duration=payout.get("best_candidate")
    timing_rows={(x.get("strategy_id"),int(x.get("duration_ticks") or 0)):x for x in payout.get("strategies",[])}
    volatility_rows={(x.get("strategy_id"),int(x.get("duration_ticks") or 0)):x for x in timing.get("strategies",[])}
    current_regime=timing.get("current_volatility_regime")
    current_volatility_row=volatility_rows.get((strategy_id,1),{})
    current_regime_evidence=next((x for x in current_volatility_row.get("volatility_regimes",[])
                                  if x.get("regime")==current_regime),None)
    if current_regime_evidence:
        val=current_regime_evidence.get("validation") or {}
        final=current_regime_evidence.get("final_test") or {}
        volatility_checks=[
            val.get("n",0)>=1000,
            bool(val.get("hit_rate_ci95") and val["hit_rate_ci95"][0]>.10),
            val.get("holm_adjusted_p",1)<.05,
            final.get("n",0)>=1000,
            bool(final.get("hit_rate_ci95") and final["hit_rate_ci95"][0]>.10),
            final.get("holm_adjusted_p",1)<.05,
        ]
        volatility_filter={
            "regime":current_regime,
            "status":"PASS" if current_regime_evidence.get("status")=="CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON" else "FAIL",
            "health_percent":round(100*sum(volatility_checks)/len(volatility_checks)),
            "checks_passed":sum(volatility_checks),
            "checks_total":len(volatility_checks),
        }
    else:
        volatility_filter={"regime":current_regime,"status":"INSUFFICIENT DATA","health_percent":None,
                           "checks_passed":0,"checks_total":6}
    digit_output=[]
    now=time.time()
    for digit in range(10):
        evidence_row=rows_by_digit.get(digit,{})
        validation=evidence_row.get("validation") or {}
        current_probability=None
        if current_model and digit<len(current_model.get("probabilities",[])):
            current_probability=float(current_model["probabilities"][digit])
        quote_by_duration={}
        for duration in (1,2,3,4,5):
            quote=None
            quote_forecast=None
            for forecast in latest_quotes:
                if int(forecast["prediction"])!=digit:
                    continue
                candidate=store.duration_quote(forecast["seq"],duration)
                if candidate:
                    quote=candidate
                    quote_forecast=forecast
                    break
            valid=bool(quote and quote_forecast
                       and int(quote.get("quote_spot_time") or 0)==int(quote_forecast["forecast_epoch"])
                       and now<int(quote_forecast["forecast_epoch"])+1)
            probability=current_probability
            ev=break_even=None
            if valid and probability is not None:
                try:
                    stake=Decimal(str(quote["stake"])); payout_value=Decimal(str(quote["payout"]))
                    ev=str(Decimal(str(probability))*payout_value-stake)
                    break_even=str(stake/payout_value) if payout_value else None
                except (InvalidOperation,KeyError,TypeError,ValueError,ZeroDivisionError):
                    valid=False
            duration_history=duration_stats[(digit,duration)]
            historical=timing_rows.get((strategy_id,duration),{})
            vol_candidate=volatility_rows.get((strategy_id,duration),{})
            current_regime=timing.get("current_volatility_regime")
            volatility_confirmed=any(x.get("regime")==current_regime and x.get("status")=="CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON"
                                     for x in vol_candidate.get("volatility_regimes",[]))
            duration_gate=bool(best_duration
                                and best_duration.get("strategy_id")==strategy_id
                                and int(best_duration.get("duration_ticks") or 0)==duration
                                and historical.get("payout_ev_status")=="OUT_OF_SAMPLE_PROFIT_EVIDENCE")
            reasons=list(evidence_row.get("gate_reasons") or [])
            if not approved:
                reasons.append("This strategy has not passed the separate strategy approval file check.")
            if not current_model or int(current_model.get("predicted_digit",-1))!=digit:
                reasons.append("The development-selected live forecast does not predict this digit.")
            if not valid:
                reasons.append("No contemporaneous unexpired Deriv proposal quote is available for this digit and duration.")
            if ev is None or Decimal(ev)<=0:
                reasons.append("The current model probability does not show positive expected value at this quote.")
            if duration!=1:
                reasons.append("Per-digit untouched-test evidence has not been frozen for this duration.")
            if not duration_gate:
                reasons.append("This strategy-duration pair has no qualifying quote-backed final-test result.")
            if not volatility_confirmed:
                reasons.append("Volatility compatibility is not confirmed for this strategy and duration.")
            limits_ok=bool(valid and quote and paperbot.limits_ok(symbol,Decimal(str(quote["stake"]))))
            if not limits_ok:
                reasons.append("Current paper risk limits do not allow this stake.")
            qualified=bool(evidence_row.get("evidence_qualified") and approved and current_model
                           and int(current_model.get("predicted_digit",-1))==digit
                           and valid and ev is not None and Decimal(ev)>0 and duration==1
                           and duration_gate and volatility_confirmed and limits_ok)
            quote_by_duration[str(duration)]={
                "historical":{"n":duration_history["n"],"hit_rate":duration_history["hit_rate"],
                              "net_pnl":duration_history["net_pnl"],"mean_return_on_stake":duration_history["mean_return_on_stake"],
                              "out_of_sample_status":historical.get("payout_ev_status","RESEARCH_ONLY")},
                "current_quote":{"available":bool(quote),"valid":valid,
                                 "stake":quote.get("stake") if quote else None,
                                 "payout":quote.get("payout") if quote else None,
                                 "break_even_probability":break_even,"expected_value":ev,
                                 "spot_time":quote.get("quote_spot_time") if quote else None},
                "decision":"PAPER TRADE CANDIDATE" if qualified else "DO NOT TRADE",
                "reasons":[] if qualified else list(dict.fromkeys(reasons)),
            }
        digit_output.append({
            "digit":digit,"estimated_win_probability":current_probability,
            "historical_observed_match_rate":validation.get("hit_rate"),
            "confidence_interval_95":validation.get("hit_rate_ci95"),
            "health_percent":evidence_row.get("health_score") if evidence_row else None,
            "health_components":evidence_row.get("health_components",{}),
            "sample_size":validation.get("n",0),
            "quoted_sample_size":validation.get("payout_adjusted",{}).get("n_with_actual_quote",0),
            "current_frequency":counts[digit]/len(history) if history else None,
            "recent_frequency":recent_counts[digit]/len(recent) if recent else None,
            "validation":validation,"final_test":evidence_row.get("final_test"),
            "recent_performance":evidence_row.get("recent"),
            "walk_forward_folds":evidence_row.get("walk_forward_folds",[]),
            "volatility_regimes":evidence_row.get("volatility_regimes",[]),
            "durations":quote_by_duration,
            "decision":"PAPER TRADE CANDIDATE" if any(x["decision"]=="PAPER TRADE CANDIDATE" for x in quote_by_duration.values()) else "DO NOT TRADE",
            "reasons":list(dict.fromkeys(
                list(evidence_row.get("gate_reasons") or [])
                +[reason for duration_data in quote_by_duration.values() for reason in duration_data["reasons"]]
            )),
        })
    settled=[x for x in store.paper_events(symbol) if x.get("event_type")=="PAPER_SETTLE"]
    def performance_groups(key):
        groups={}
        for row in settled:
            name=str(row.get(key) if row.get(key) is not None else "unknown")
            value=groups.setdefault(name,{"contracts":0,"wins":0,"net_pnl":0.0})
            value["contracts"]+=1
            value["wins"]+=int(row.get("hit") or 0)
            value["net_pnl"]+=float(row.get("paper_pnl") or 0)
        for value in groups.values():
            value["win_rate"]=value["wins"]/value["contracts"] if value["contracts"] else None
            value["net_pnl"]=round(value["net_pnl"],8)
        return groups
    tick_stale=bool(collector.last_received.get(symbol) is None or now-collector.last_received[symbol]>settings.max_data_age_seconds)
    market_health=0 if tick_stale or len(history)<2 else round(100*sum(int(b["epoch"])-int(a["epoch"])==1 for a,b in zip(history[:-1],history[1:]))/(len(history)-1))
    strategies_by_id={x.get("strategy_id"):x for x in report_item.get("strategies",[])}
    strategy_output=[]
    for candidate in signal_data.get("signals",[]):
        result=strategies_by_id.get(candidate["strategy_id"],{})
        validation=result.get("validation") or {}
        strategy_output.append({
            "strategy_id":candidate["strategy_id"],"version":candidate["version"],
            "predicted_digit":candidate["predicted_digit"],
            "predicted_digit_probability":candidate["predicted_digit_probability"],
            "probabilities":candidate["probabilities"],
            "validation_sample_size":validation.get("n"),
            "validation_hit_rate":validation.get("hit_rate"),
            "validation_brier":validation.get("brier_multiclass_10class"),
            "validation_calibration":validation.get("reliability",[]),
            "validation_status":result.get("validation_verdict","INSUFFICIENT_DATA"),
            "final_test_status":result.get("final_test_verdict") or result.get("final_test_status"),
            "selected":candidate["strategy_id"]==strategy_id,
            "explicitly_approved":paperbot.approved(symbol,candidate["strategy_id"]),
        })
    return {
        "symbol":symbol,"mode":"PAPER_ONLY","trade_authorized":False,
        "evaluated_at":time.time(),"selected_strategy_id":strategy_id,
        "selection_basis":evidence.get("selection_basis"),
        "health_definition":evidence.get("health_definition"),
        "market_health_percent":market_health,"market_feed_stale":tick_stale,
        "latest_tick":dict(history[-1]) if history else None,
        "volatility":{"regime":timing.get("current_volatility_regime"),
                      "reading":timing.get("current_volatility"),
                      "short_term":timing.get("short_term_volatility"),
                      "long_term":timing.get("long_term_volatility")},
        "volatility_filter":volatility_filter,
        "best_duration_candidate":best_duration,
        "strategies":strategy_output,"digits":digit_output,
        "paper_performance":{"by_digit":performance_groups("prediction"),
                             "by_strategy":performance_groups("strategy_id"),
                             "settled_contracts":len(settled)},
        "note":"A digit's health is a research evidence score, not a win probability. Every displayed estimate comes from the current model or recorded outcomes. TRADE status is paper-only and requires all evidence, quote, timing, approval and risk gates.",
    }

@app.get("/decision")
def unified_decision(symbol: str):
    """Return one timestamped, internally checked paper-only decision snapshot."""
    from datetime import datetime, timezone
    if symbol not in settings.symbols:
        raise HTTPException(404,"Symbol not configured")
    snapshot_id=str(uuid4())
    snapshot_timestamp=datetime.now(timezone.utc).isoformat()
    initial_ticks=store.quality_history(symbol,1)
    initial_tick_id=initial_ticks[-1]["id"] if initial_ticks else None
    report_data=report()
    report_item=next((item for item in report_data.get("symbols",[]) if item.get("symbol")==symbol),{})
    digit_data=digit_analysis(symbol)
    signal_data=signals(symbol)
    timing_data=timing_research(symbol)
    ready=ready_signal(symbol)
    quality_ticks=store.quality_history(symbol,21)
    paper_events=store.paper_events(symbol)
    forecast_rows=store.forecasts(symbol,limit=5000)
    prior_runs=store.agent_runs(symbol,limit=20)
    final_tick=store.quality_history(symbol,1)
    final_tick_id=final_tick[-1]["id"] if final_tick else None
    current_signal_tick_id=signal_data.get("observed_tick_id")
    digit_tick=(digit_data.get("latest_tick") or {}).get("id")
    consistent=bool(
        initial_tick_id is not None
        and initial_tick_id==final_tick_id
        and current_signal_tick_id==initial_tick_id
        and digit_tick==initial_tick_id
    )
    feed_stale=(
        collector.last_received.get(symbol) is None
        or time.time()-collector.last_received[symbol]>settings.max_data_age_seconds
    )
    coordinated=coordinate_snapshot(
        report_item=report_item,
        digit_analysis=digit_data,
        signal_data=signal_data,
        timing=timing_data,
        ready=ready,
        quality_ticks=quality_ticks,
        feed_stale=feed_stale,
        max_data_age_seconds=settings.max_data_age_seconds,
        collector_errors=collector.errors,
        paper_events=paper_events,
        forecasts=forecast_rows,
        prior_runs=prior_runs,
        consistent=consistent,
        snapshot_timestamp=snapshot_timestamp,
    )
    decision_data=coordinated["decision"]
    risk_result=next(item for item in coordinated["agents"] if item["agent_id"]=="risk_signal_approval")
    approved_signal=decision_data.get("signal") if decision_data.get("trade_authorized") else None
    decision_key="|".join(str(value) for value in (
        decision_data.get("final_status"),
        (approved_signal or {}).get("signal_id"),
        (approved_signal or {}).get("predicted_digit"),
        (approved_signal or {}).get("duration"),
        (approved_signal or {}).get("probability"),
    ))
    persisted=store.record_agent_run(
        snapshot_id=snapshot_id,
        symbol=symbol,
        snapshot_tick_id=final_tick_id,
        final_status=decision_data.get("final_status","NO TRADE — INSUFFICIENT EVIDENCE"),
        decision_key=decision_key,
        agent_results=coordinated["agents"],
    )
    signal_digit=int(approved_signal["predicted_digit"]) if approved_signal else None
    digit_predictions=[]
    for row in digit_data.get("digits",[]):
        is_approved=bool(
            approved_signal
            and int(row["digit"])==signal_digit
            and row.get("decision")=="PAPER TRADE CANDIDATE"
        )
        rejection_reasons=list(row.get("reasons") or [])
        if not is_approved and row.get("decision")=="PAPER TRADE CANDIDATE":
            rejection_reasons.extend(risk_result["reasons"])
        digit_predictions.append({
            "digit":int(row["digit"]),
            "estimated_win_probability":row.get("estimated_win_probability"),
            "health_percent":row.get("health_percent"),
            "decision":"TRADE" if is_approved else "NO TRADE",
            "reasons":[] if is_approved else list(dict.fromkeys(rejection_reasons or ["No fully qualified paper signal for this digit."])),
        })
    data_agent=next(item for item in coordinated["agents"] if item["agent_id"]=="data_intelligence")
    regime_agent=next(item for item in coordinated["agents"] if item["agent_id"]=="market_regime")
    pipeline=_pipeline_state(symbol)
    return {
        "snapshot_id":snapshot_id,
        "created_at":snapshot_timestamp,
        "symbol":symbol,
        "currency":settings.currency,
        "mode":"PAPER_ONLY",
        "snapshot_consistent":consistent,
        "snapshot_tick_id":final_tick_id,
        "latest_tick":final_tick[-1] if final_tick else None,
        "final_status":decision_data.get("final_status"),
        "trade_authorized":bool(decision_data.get("trade_authorized")),
        "signal":approved_signal,
        "reasons":risk_result["reasons"],
        "market_status":{
            "connected":pipeline["connection_status"]=="CONNECTED",
            "connection_status":pipeline["connection_status"],
            "subscription_status":pipeline["subscription_status"],
            "data_status":pipeline["data_status"],
            "pipeline_status":pipeline["pipeline_status"],
            "receiving_data":pipeline["data_status"]=="RECEIVING DATA",
            "data_quality_status":data_agent["status"],
            "freshness_seconds":data_agent["metrics"].get("latest_tick_age_seconds"),
            "latest_tick":final_tick[-1] if final_tick else None,
        },
        "pipeline":pipeline,
        "digits":digit_predictions,
        "volatility":{
            "condition":regime_agent["metrics"].get("current_regime"),
            "health_percent":regime_agent["metrics"].get("volatility_health_percent"),
            "status":regime_agent["status"],
        },
        "agents":coordinated["agents"],
        "performance":coordinated["performance"],
        "agent_run_persisted":persisted,
    }

@app.get("/signals")
def signals(symbol: str):
    """Current candidate forecasts for inspection only; never a trade authorization."""
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    history=store.history(symbol,limit=1000)
    if len(history) < settings.min_history_ticks:
        return {"symbol":symbol,"status":"insufficient_history","required_ticks":settings.min_history_ticks,"observed_ticks":len(history),"signals":[],"trade_authorized":False}
    digits=tuple(int(r["digit"]) for r in history)
    counts=store.digit_counts(symbol)
    latest=history[-1]
    context=ForecastContext(symbol,int(latest["id"]),int(latest["epoch"]),digits,tuple(int(x) for x in counts))
    forecasts=[strategy.forecast(context) for strategy in candidate_strategies()]
    output=[]
    for f in forecasts:
        output.append({"strategy_id":f.strategy_id,"version":f.version,"parameters":f.parameters,
                       "predicted_digit":f.prediction,"predicted_digit_probability":f.probabilities[f.prediction],
                       "probabilities":list(f.probabilities),"history_support":len(digits),
                       "status":"RESEARCH_ONLY_NOT_APPROVED"})
    output.sort(key=lambda x:x["predicted_digit_probability"],reverse=True)
    return {"symbol":symbol,"observed_tick_id":int(latest["id"]),"observed_epoch":int(latest["epoch"]),
            "signals":output,"trade_authorized":False,
            "note":"Ranking is descriptive only. It is not a calibrated confidence score and does not bypass validation, quote, or paper-risk gates."}

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    options="".join(f'<option value="{s}">{s}</option>' for s in settings.symbols)
    page=(Path(__file__).parent / "dashboard_minimal.html").read_text(encoding="utf-8")
    return page.replace("__SYMBOL_OPTIONS__",options)

@app.get("/advanced", response_class=HTMLResponse)
def advanced_dashboard():
    options="".join(f'<option value="{s}">{s}</option>' for s in settings.symbols)
    page=(Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    return (page.replace("__SYMBOL_OPTIONS__",options)
            .replace("__SYMBOLS_JSON__",json.dumps(list(settings.symbols)))
            .replace("</header>",'<a href="/dashboard" style="color:#9ab9c8;margin-left:12px">Decision terminal</a></header>'))
```

## `bromax/cli.py`

```python
import argparse, json
from .config import Settings
from .storage import Store
from .reporting import write_report

def main():
    p=argparse.ArgumentParser(prog="bromax"); sub=p.add_subparsers(dest="cmd",required=True)
    r=sub.add_parser("report"); r.add_argument("--output",default="reports/bromax_report.json")
    args=p.parse_args(); s=Settings.from_env(); store=Store(s.database_path)
    if args.cmd=="report":
        report=write_report(store,s.symbols,args.output); print(json.dumps(report,indent=2,default=str)); print(f"Report written: {args.output}")
if __name__=="__main__": main()
```

## `bromax/collector.py`

```python
import asyncio, json, logging, time
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock
import websockets
from .digits import extract_last_digit, quote_precision, QuotePrecisionError
from .strategies import candidate_strategies, ForecastContext
from .proposals import ProposalClient, ProposalUnavailable
from .paperbot import PaperBot

log=logging.getLogger("bromax.collector")

class MarketCollector:
    def __init__(self, settings, store, paperbot=None):
        self.settings=settings; self.store=store; self.paperbot=paperbot; self.last_received={}; self.errors=[]
        self.proposals=ProposalClient(settings.ws_url,settings.currency,settings.proposal_timeout_seconds)
        self.proposal_semaphore=asyncio.Semaphore(3)
        self.quote_tasks=set()
        self.cumulative_counts={symbol:list(store.digit_counts(symbol)) for symbol in settings.symbols}
        self.strategies=candidate_strategies()
        self._status_lock=Lock()
        self.connection_status="DISCONNECTED"
        self.connection_started_monotonic=None
        self.reconnect_count=0
        self.connection_error=None
        self._symbol_status={
            symbol:{
                "subscription_status":"NOT_SUBSCRIBED",
                "subscription_count":0,
                "received_ticks":0,
                "processed_ticks":0,
                "duplicate_ticks":0,
                "invalid_ticks":0,
                "out_of_order_ticks":0,
                "missing_tick_count":0,
                "history_seeded_ticks":0,
                "analysis_status":"WAITING_FOR_DATA",
                "analysis_runs":0,
                "strategy_status":"WAITING_FOR_DATA",
                "strategy_runs":0,
                "signal_generation_status":"WAITING_FOR_DATA",
                "signals_generated":0,
                "last_tick_epoch":None,
                "last_tick_id":None,
                "last_analysis_at":None,
                "last_signal_at":None,
                "last_signal_epoch":None,
                "last_error":None,
                "processing_errors":0,
            }
            for symbol in settings.symbols
        }

    def _record_error(self, message, symbol=None):
        self.errors.append(str(message))
        if len(self.errors)>100: del self.errors[:-100]
        with self._status_lock:
            if symbol in self._symbol_status:
                status=self._symbol_status[symbol]
                status["last_error"]=str(message)
                status["processing_errors"]+=1

    def _update_symbol_status(self, symbol, **values):
        with self._status_lock:
            self._symbol_status[symbol].update(values)

    def _increment_symbol_status(self, symbol, key, amount=1):
        with self._status_lock:
            status=self._symbol_status[symbol]
            status[key]+=amount

    def _set_connection_status(self, status, error=None, reconnect=False):
        now=datetime.now(timezone.utc).isoformat()
        with self._status_lock:
            self.connection_status=status
            self.connection_error=error
            if reconnect:
                self.reconnect_count+=1
            for symbol_status in self._symbol_status.values():
                symbol_status["connection_status"]=status
                if status in {"CONNECTING","RECONNECTING","DISCONNECTED","ERROR"}:
                    symbol_status["subscription_status"]="NOT_SUBSCRIBED"
                if status=="CONNECTED":
                    symbol_status["connected_at"]=now
                    symbol_status["last_error"]=None
                elif error:
                    symbol_status["last_error"]=error

    def operational_status(self, symbol):
        if symbol not in self._symbol_status:
            raise KeyError(symbol)
        with self._status_lock:
            result=dict(self._symbol_status[symbol])
            result["connection_status"]=self.connection_status
            result["reconnect_count"]=self.reconnect_count
            result["connection_error"]=self.connection_error
        received=self.last_received.get(symbol)
        age=max(0.0,time.time()-received) if received is not None else None
        result["freshness_seconds"]=round(age,3) if age is not None else None
        result["data_status"]=(
            "NO DATA" if age is None else
            "STALE" if age>self.settings.max_data_age_seconds else
            "RECEIVING DATA"
        )
        if result["connection_status"]=="RECONNECTING":
            result["pipeline_status"]="RECONNECTING"
        elif result.get("last_error"):
            result["pipeline_status"]="ERROR"
        elif result["data_status"]=="STALE":
            result["pipeline_status"]="STALE DATA"
        elif result["subscription_status"]!="SUBSCRIBED":
            result["pipeline_status"]=result["connection_status"]
        elif result["signal_generation_status"]=="GENERATING SIGNALS":
            result["pipeline_status"]="GENERATING SIGNALS"
        elif result["analysis_status"]=="ANALYZING":
            result["pipeline_status"]="ANALYZING"
        else:
            result["pipeline_status"]="RECEIVING DATA"
        return result

    def _seed_history(self, symbol, history, pip_size):
        """Persist a Deriv ticks_history response without generating retrospective forecasts."""
        prices=history.get("prices") or []
        times=history.get("times") or []
        precision=quote_precision(pip_size)
        if len(prices)!=len(times):
            raise ValueError(f"Historical seed arrays have mismatched lengths for {symbol}")
        inserted=0
        ordered=sorted(zip(times,prices),key=lambda item:int(item[0]))
        for epoch,price in ordered:
            epoch=int(epoch)
            quote_text,digit=extract_last_digit(price,precision)
            if digit is not None and self.store.add_tick(symbol,epoch,quote_text,precision,digit) is not None:
                inserted += 1
        self._increment_symbol_status(symbol,"history_seeded_ticks",inserted)
        return inserted
    async def _handle_tick(self, tick, symbol_hint=None):
        symbol=tick.get("symbol") or symbol_hint
        if symbol not in self.settings.symbols: raise ValueError(f"Unexpected symbol {symbol!r}")
        self._increment_symbol_status(symbol,"received_ticks")
        self._update_symbol_status(symbol,subscription_status="SUBSCRIBED")
        try:
            if "quote" not in tick or "epoch" not in tick or "pip_size" not in tick:
                raise ValueError("Tick missing quote/epoch/pip_size; refusing to infer")
            epoch=int(tick["epoch"])
            precision=quote_precision(tick["pip_size"])
            quote_text,digit=extract_last_digit(tick["quote"],precision)
            if digit is None: raise ValueError(f"Symbol {symbol} has no displayed decimal digit for MATCH contracts")
        except (QuotePrecisionError,ValueError,TypeError):
            self._increment_symbol_status(symbol,"invalid_ticks")
            raise
        previous=self.store.history(symbol,limit=1)
        previous_epoch=int(previous[-1]["epoch"]) if previous else None
        if previous_epoch is not None and epoch<previous_epoch:
            self._increment_symbol_status(symbol,"out_of_order_ticks")
            return False
        if previous_epoch is not None and epoch==previous_epoch:
            self._increment_symbol_status(symbol,"duplicate_ticks")
            return False
        if previous_epoch is not None and epoch>previous_epoch+1:
            self._increment_symbol_status(symbol,"missing_tick_count",epoch-previous_epoch-1)
        tick_id=self.store.add_tick(symbol,epoch,quote_text,precision,digit,tick.get("id"))
        if tick_id is None:
            self._increment_symbol_status(symbol,"duplicate_ticks")
            return False
        self.last_received[symbol]=time.time()
        self._increment_symbol_status(symbol,"processed_ticks")
        self._update_symbol_status(
            symbol,
            last_error=None,
            last_tick_epoch=int(tick["epoch"]),
            last_tick_id=tick_id,
            analysis_status="WAITING_FOR_HISTORY",
            strategy_status="WAITING_FOR_HISTORY",
            signal_generation_status="WAITING_FOR_HISTORY",
        )
        self.cumulative_counts[symbol][digit] += 1
        # First score prior forecasts with this newly arrived tick; then forecast from history ending here.
        self.store.score_open_forecasts(symbol,tick_id,int(tick["epoch"]),digit)
        self.store.settle_duration_contracts(symbol,tick_id,int(tick["epoch"]),digit)
        history=self.store.history(symbol,limit=1000)
        digits=tuple(int(r["digit"]) for r in history)
        if len(digits)<self.settings.min_history_ticks:
            return True
        latest=history[-1]
        ctx=ForecastContext(symbol,int(latest["id"]),int(latest["epoch"]),digits,tuple(self.cumulative_counts[symbol]))
        self._update_symbol_status(
            symbol,
            analysis_status="ANALYZING",
            strategy_status="ANALYZING",
            signal_generation_status="GENERATING SIGNALS",
        )
        self._increment_symbol_status(symbol,"analysis_runs")
        forecasts=[s.forecast(ctx) for s in self.strategies]
        # Freeze all forecasts synchronously before returning to the stream reader.
        # Quote requests run in background and are separately timestamped append-only events.
        forecast_rows=[]
        for f in forecasts:
            seq=self.store.append_forecast(symbol,f,tick_id,int(tick["epoch"]),None,self.settings.paper_stake,None,"PAYOUT_PENDING")
            forecast_rows.append((f,seq))
        generated_at=datetime.now(timezone.utc).isoformat()
        self._update_symbol_status(
            symbol,
            analysis_status="ANALYZED",
            strategy_status="PROCESSED",
            signal_generation_status="GENERATED" if forecasts else "NO SIGNALS",
            last_analysis_at=generated_at,
            last_signal_at=generated_at if forecasts else None,
            last_signal_epoch=int(tick["epoch"]) if forecasts else None,
        )
        self._increment_symbol_status(symbol,"strategy_runs",len(forecasts))
        self._increment_symbol_status(symbol,"signals_generated",len(forecasts))
        if len(self.quote_tasks) >= self.settings.max_pending_quote_tasks:
            self._record_error("Proposal queue full; forecast retained without payout quote")
            log.warning("Proposal queue full; skipping quote requests for %s tick %s",symbol,tick_id)
        else:
            task=asyncio.create_task(self._capture_quotes(symbol,tick_id,int(tick["epoch"]),forecast_rows))
            self.quote_tasks.add(task)
            task.add_done_callback(self.quote_tasks.discard)
        return True

    async def _capture_quotes(self, symbol, tick_id, epoch, forecast_rows):
        unique=sorted({f.prediction for f,_ in forecast_rows})
        async def getq(d):
            try:
                async with self.proposal_semaphore:
                    return d, await self.proposals.quote(symbol,d,self.settings.paper_stake,1)
            except ProposalUnavailable as e:
                self._record_error(f"Proposal unavailable for {symbol} digit {d}: {e}")
                log.warning("No proposal for %s digit %s: %s",symbol,d,e); return d,None
        try:
            # Request actual Deriv proposal quotes for every candidate digit and
            # horizon over one WebSocket session to minimize quote latency.
            requests=[(symbol,d,self.settings.paper_stake,h) for d in unique for h in (1,2,3,4,5)]
            try:
                quotes=await self.proposals.quote_many(requests)
            except ProposalUnavailable as exc:
                self._record_error(f"Duration proposal batch unavailable for {symbol}: {exc}")
                quotes={}
            for f,seq in forecast_rows:
                for horizon in (1,2,3,4,5):
                    q=quotes.get((symbol,int(f.prediction),int(horizon)))
                    if not q: continue
                    try:
                        # Preserve legacy one-tick quote/paper path. Multi-tick
                        # quote/outcome pairs are tracked in their own immutable ledger.
                        if horizon == 1:
                            self.store.append_quote(symbol,f,seq,q["payout"],q["stake"],q["proposal_id"],q.get("spot_time"))
                            if self.paperbot:
                                self.paperbot.record(symbol,f,seq,tick_id,epoch,q["stake"],q["payout"],q["proposal_id"],q["spot_time"])
                        self.store.append_duration_quote(symbol,f,seq,tick_id,epoch,horizon,q["stake"],q["payout"],q["proposal_id"],q.get("spot_time"))
                    except Exception as exc:
                        self._record_error(f"Duration quote persistence failed for {symbol} forecast {seq} horizon {horizon}: {exc}")
                        log.exception("Could not persist duration quote")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(f"Quote capture task failed for {symbol} tick {tick_id}: {exc}")
            log.exception("Quote capture task failed")
    async def run_forever(self):
        delay=1
        try:
            while True:
                connected_at=None
                try:
                    self._set_connection_status("CONNECTING")
                    async with websockets.connect(self.settings.ws_url,ping_interval=20,ping_timeout=20,close_timeout=10) as ws:
                        connected_at=time.monotonic()
                        self.connection_started_monotonic=connected_at
                        self._set_connection_status("CONNECTED")
                        req_symbols={}; subscription_symbols={}
                        # Seed from the latest historical ticks without generating retrospective forecasts.
                        # This avoids waiting 1,000 live seconds before the research engine can start.
                        history_requests={200+index:symbol for index,symbol in enumerate(self.settings.symbols)}
                        history_status={symbol:"PENDING" for symbol in self.settings.symbols}
                        pending=set(history_requests)
                        for req_id,symbol in history_requests.items():
                            await ws.send(json.dumps({"ticks_history":symbol,"count":1000,"end":"latest","style":"ticks","req_id":req_id}))
                        while pending:
                            try:
                                raw_history=await asyncio.wait_for(ws.recv(),timeout=8)
                            except asyncio.TimeoutError:
                                self._record_error("Historical seed timed out; continuing with live ticks only")
                                break
                            symbol=None
                            try:
                                seed_msg=json.loads(raw_history,parse_float=Decimal)
                                seed_id=seed_msg.get("req_id")
                                if seed_id not in pending:
                                    continue
                                pending.remove(seed_id)
                                symbol=history_requests[seed_id]
                                if seed_msg.get("error"):
                                    history_status[symbol]="ERROR"
                                    self._update_symbol_status(symbol,history_seed_status="ERROR")
                                    error_message=seed_msg["error"].get("message","Deriv error")
                                    self._record_error(f"Historical seed failed for {symbol}: {error_message}",symbol)
                                    continue
                                hist=seed_msg.get("history") or {}
                                inserted=self._seed_history(symbol,hist,seed_msg.get("pip_size"))
                                history_status[symbol]="READY"
                                self._update_symbol_status(symbol,history_seed_status="READY")
                                log.info("Seeded %s historical ticks for %s",inserted,symbol)
                            except (json.JSONDecodeError,QuotePrecisionError,ValueError,KeyError,TypeError) as exc:
                                if symbol is not None:
                                    history_status[symbol]="ERROR"
                                    self._update_symbol_status(symbol,history_seed_status="ERROR")
                                self._record_error(f"Historical seed rejected: {exc}",symbol)
                                log.exception("Could not seed historical ticks")
                        for symbol in self.settings.symbols:
                            self.cumulative_counts[symbol]=list(self.store.digit_counts(symbol))
                            if symbol in pending:
                                history_status[symbol]="TIMEOUT"
                            if history_status[symbol]!="READY":
                                self._update_symbol_status(symbol,history_seed_status=history_status[symbol])
                        for index,symbol in enumerate(self.settings.symbols):
                            req_id=100+index; req_symbols[req_id]=symbol
                            self._update_symbol_status(
                                symbol,
                                subscription_status="SUBSCRIBING",
                            )
                            self._increment_symbol_status(symbol,"subscription_count")
                            await ws.send(json.dumps({"ticks":symbol,"subscribe":1,"req_id":req_id}))
                        # A normal WebSocket close is also a disconnect; route it through
                        # the same bounded backoff path as network errors.
                        async for raw in ws:
                            try:
                                msg=json.loads(raw,parse_float=Decimal)
                                if msg.get("error"):
                                    self._record_error(msg["error"].get("message","Deriv error"))
                                    log.error("Deriv stream error: %s",msg["error"])
                                    continue
                                tick=msg.get("tick")
                                if msg.get("msg_type")=="tick" and tick:
                                    req_id=msg.get("req_id")
                                    subscription_id=(msg.get("subscription") or {}).get("id")
                                    hint=(tick.get("symbol") or req_symbols.get(req_id)
                                          or (msg.get("echo_req") or {}).get("ticks")
                                          or subscription_symbols.get(subscription_id)
                                          or subscription_symbols.get(tick.get("id")))
                                    if hint:
                                        if subscription_id: subscription_symbols[subscription_id]=hint
                                        if tick.get("id"): subscription_symbols[tick["id"]]=hint
                                    try:
                                        await self._handle_tick(tick,hint)
                                    except (QuotePrecisionError,ValueError,KeyError,TypeError) as exc:
                                        self._record_error(f"Rejected malformed tick: {exc}",hint)
                                        log.exception("Rejected malformed tick: %s",exc)
                                    except Exception as exc:
                                        self._record_error(f"Tick processing failed: {exc}",hint)
                                        self._update_symbol_status(
                                            hint,
                                            analysis_status="ERROR",
                                            strategy_status="ERROR",
                                            signal_generation_status="ERROR",
                                        )
                                        log.exception("Tick processing failed")
                            except (json.JSONDecodeError,QuotePrecisionError,ValueError,KeyError,TypeError) as exc:
                                self._record_error(exc)
                                log.exception("Rejected malformed tick: %s",exc)
                    if connected_at is not None and time.monotonic()-connected_at>=60:
                        delay=1
                    self._set_connection_status("RECONNECTING","Deriv closed the tick stream",reconnect=True)
                    log.warning("Deriv closed the tick stream; reconnecting in %ss",delay)
                    await asyncio.sleep(delay)
                    delay=min(delay*2,30)
                except asyncio.CancelledError:
                    self._set_connection_status("DISCONNECTED")
                    raise
                except Exception as exc:
                    self._record_error(exc)
                    if connected_at is not None and time.monotonic()-connected_at>=60:
                        delay=1
                    self._set_connection_status("RECONNECTING",str(exc),reconnect=True)
                    log.exception("Market stream disconnected; reconnecting in %ss",delay)
                    await asyncio.sleep(delay)
                    delay=min(delay*2,30)
                finally:
                    self.connection_started_monotonic=None
        finally:
            if self.connection_status!="DISCONNECTED":
                self._set_connection_status("DISCONNECTED")
            for task in tuple(self.quote_tasks): task.cancel()
            if self.quote_tasks:
                await asyncio.gather(*tuple(self.quote_tasks),return_exceptions=True)
```

## `bromax/config.py`

```python
from dataclasses import dataclass
from decimal import Decimal
import os
import re

@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...] = ("1HZ100V",)
    ws_url: str = "wss://api.derivws.com/trading/v1/options/ws/public"
    database_path: str = "data/bromax.sqlite3"
    currency: str = "USD"
    paper_stake: Decimal = Decimal("1.00")
    max_paper_stake: Decimal = Decimal("1.00")
    daily_loss_cap: Decimal = Decimal("10.00")
    max_trades_per_day: int = 100
    max_data_age_seconds: int = 5
    min_history_ticks: int = 1000
    proposal_timeout_seconds: float = 4.0
    max_pending_quote_tasks: int = 20

    def __post_init__(self):
        if not self.symbols or any(not re.fullmatch(r"[A-Za-z0-9_]{2,30}", x) for x in self.symbols):
            raise ValueError("DERIV_SYMBOLS must contain one or more valid Deriv symbol codes")
        if any(not x.startswith("1HZ") for x in self.symbols):
            raise ValueError("This protocol scores the immediately following one-second tick; configure only 1HZ symbols")
        if not self.currency or not re.fullmatch(r"[A-Za-z0-9]{2,20}", self.currency):
            raise ValueError("CURRENCY must be a valid 2–20 character currency code")
        if self.paper_stake <= 0 or self.max_paper_stake <= 0 or self.paper_stake > self.max_paper_stake:
            raise ValueError("Paper stake must be positive and no greater than MAX_PAPER_STAKE")
        if self.daily_loss_cap <= 0 or self.max_trades_per_day < 1:
            raise ValueError("Daily loss cap and max trades must be positive")
        if self.max_data_age_seconds < 1 or self.min_history_ticks < 1000 or self.proposal_timeout_seconds <= 0 or self.max_pending_quote_tasks < 1:
            raise ValueError("Data age, proposal timeout, and minimum 1,000-tick history must be valid")

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        symbols = tuple(x.strip() for x in os.getenv("DERIV_SYMBOLS", "1HZ100V").split(",") if x.strip())
        return cls(
            symbols=symbols,
            ws_url=os.getenv("DERIV_WS_URL", cls.ws_url),
            database_path=os.getenv("DATABASE_PATH", cls.database_path),
            currency=os.getenv("CURRENCY", "USD"),
            paper_stake=Decimal(os.getenv("PAPER_STAKE", "1.00")),
            max_paper_stake=Decimal(os.getenv("MAX_PAPER_STAKE", "1.00")),
            daily_loss_cap=Decimal(os.getenv("DAILY_LOSS_CAP", "10.00")),
            max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "100")),
            max_data_age_seconds=int(os.getenv("MAX_DATA_AGE_SECONDS", "5")),
            min_history_ticks=int(os.getenv("MIN_HISTORY_TICKS", "1000")),
            proposal_timeout_seconds=float(os.getenv("PROPOSAL_TIMEOUT_SECONDS", "4")),
            max_pending_quote_tasks=int(os.getenv("MAX_PENDING_QUOTE_TASKS", "20")),
        )
```

## `bromax/dashboard.html`

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BROMAX — Market Desk</title>
<style>
:root{color-scheme:dark;--bg:#101315;--surface:#171c1e;--surface2:#1d2426;--line:#303a3b;--line2:#263031;--text:#e8eeeb;--muted:#94a19c;--soft:#bac5c0;--green:#83c7a2;--green2:#244c3a;--amber:#d9b675;--amber2:#4a3d26;--red:#d78e88;--red2:#4a2928;--blue:#9ab9c8;--mono:ui-monospace,SFMono-Regular,Consolas,monospace;--sans:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.48 var(--sans);font-feature-settings:"tnum" 1,"ss01" 1}button,select{font:inherit;color:inherit;background:#202829;border:1px solid var(--line);border-radius:5px;padding:8px 10px}button{cursor:pointer}button:hover{background:#293334;border-color:#52605e}button:focus-visible,select:focus-visible{outline:2px solid var(--green);outline-offset:2px}.topbar{height:62px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 26px;background:#14191a;gap:18px}.brand{display:flex;align-items:center;gap:12px}.brandmark{width:28px;height:28px;border:1px solid #648d78;color:var(--green);display:grid;place-items:center;font:700 12px var(--mono);letter-spacing:-1px}.brandname{font-size:14px;font-weight:700;letter-spacing:.13em}.brandname span{color:var(--muted);font-weight:500;letter-spacing:.02em}.topmeta{display:flex;align-items:center;gap:18px;color:var(--muted);font-size:12px}.connection{display:flex;align-items:center;gap:7px}.dot{width:7px;height:7px;border-radius:50%;background:#66716d;display:inline-block}.dot.ok{background:var(--green);box-shadow:0 0 0 3px #83c7a21c}.dot.warn{background:var(--amber)}.toolbar{display:flex;align-items:center;gap:8px}.toolbar label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.toolbar select{min-width:150px}.wrap{max-width:1540px;margin:auto;padding:22px 26px 36px}.pagehead{display:flex;justify-content:space-between;align-items:flex-end;margin:2px 0 18px;gap:18px}.eyebrow{font-size:10px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);font-weight:650}.pagehead h1{font-size:23px;letter-spacing:-.035em;margin:3px 0 0;font-weight:620}.pagehead p{margin:4px 0 0;color:var(--muted);font-size:12px}.updated{color:var(--muted);font-size:11px;text-align:right}.statusline{border:1px solid var(--line);background:#151b1c;padding:9px 12px;display:flex;align-items:center;gap:10px;margin-bottom:14px;color:var(--soft);font-size:12px}.statusline strong{font-weight:650;color:var(--text)}.layout-main{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(280px,.85fr);gap:14px;align-items:stretch}.panel{background:var(--surface);border:1px solid var(--line);padding:15px 16px;min-width:0}.panelhead{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:13px}.paneltitle{font-size:12px;font-weight:680;letter-spacing:.015em}.panelnote{font-size:11px;color:var(--muted)}.signal-panel{min-height:264px;position:relative;display:flex;flex-direction:column}.signal-state{display:flex;align-items:center;gap:8px;font-size:10px;letter-spacing:.12em;font-weight:700;color:var(--amber)}.signal-state.ready{color:var(--green)}.signal-state.rejected{color:var(--red)}.signal-body{display:grid;grid-template-columns:150px minmax(0,1fr);gap:20px;align-items:center;flex:1}.digit-block{border-right:1px solid var(--line);padding:8px 22px 8px 4px}.digit-caption{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.1em}.bigdigit{font:500 74px/.95 var(--mono);letter-spacing:-.09em;margin:8px 0;color:var(--soft)}.bigdigit.ready{color:var(--green)}.digit-sub{font-size:11px;color:var(--muted)}.signal-details{display:grid;grid-template-columns:repeat(3,minmax(90px,1fr));gap:14px 18px}.detail label{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px}.detail strong{font:600 14px var(--mono);color:var(--text)}.detail small{display:block;color:var(--muted);font-size:10px;margin-top:2px}.signal-footer{display:flex;align-items:center;justify-content:space-between;border-top:1px solid var(--line2);padding-top:10px;margin-top:12px;gap:12px;color:var(--muted);font-size:11px}.signal-footer code{font:11px var(--mono);color:var(--soft)}.empty-signal{display:flex;align-items:center;gap:14px;min-height:165px}.empty-icon{width:42px;height:42px;border:1px solid var(--line);display:grid;place-items:center;color:var(--amber);font:18px var(--mono)}.empty-signal h3{font-size:14px;margin:0 0 4px;font-weight:620}.empty-signal p{margin:0;color:var(--muted);max-width:510px;font-size:12px}.side-stack{display:grid;grid-template-rows:1fr 1fr;gap:14px}.marketline{display:flex;justify-content:space-between;align-items:flex-end;margin:8px 0 12px}.price{font:500 25px var(--mono);letter-spacing:-.04em}.lastdigit{font:500 30px var(--mono);color:var(--green)}.microstats{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;border-top:1px solid var(--line2);padding-top:11px}.microstats label{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}.microstats strong{display:block;font:600 13px var(--mono);margin-top:3px}.guard-row{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid var(--line2);font-size:11px}.guard-row:last-child{border:0}.guard-row span{color:var(--muted)}.guard-row strong{font-weight:600;text-align:right}.tag{font-size:9px;letter-spacing:.08em;text-transform:uppercase;padding:3px 6px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}.tag.good{color:var(--green);border-color:#456b55}.tag.warn{color:var(--amber);border-color:#665637}.tag.bad{color:var(--red);border-color:#704441}.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));margin:14px 0;gap:0;border:1px solid var(--line);background:var(--surface)}.kpi{padding:12px 15px;border-right:1px solid var(--line)}.kpi:last-child{border:0}.kpi label{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.09em}.kpi strong{display:block;font:600 20px var(--mono);margin-top:5px;letter-spacing:-.04em}.kpi small{display:block;color:var(--muted);font-size:10px;margin-top:2px}.lower{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1.25fr) minmax(260px,.9fr);gap:14px;align-items:start}.digitrows{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 15px}.digitrow{display:grid;grid-template-columns:16px 1fr 45px;align-items:center;gap:8px;font:11px var(--mono)}.digitrow .n{color:var(--soft)}.track{height:5px;background:#293233;overflow:hidden}.track i{display:block;height:100%;background:#729c83}.digitrow .pct{text-align:right;color:var(--muted)}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th{text-align:left;color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.08em;font-weight:600;padding:7px 8px;border-bottom:1px solid var(--line)}td{padding:8px;border-bottom:1px solid var(--line2);font-size:11px;color:var(--soft)}td.num{font-family:var(--mono);font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:0}.strategy-name{color:var(--text);font-weight:600}.muted{color:var(--muted)}.activity{display:flex;flex-direction:column}.event{display:grid;grid-template-columns:56px 7px minmax(0,1fr);gap:8px;padding:8px 0;border-bottom:1px solid var(--line2);font-size:11px}.event:last-child{border:0}.event time{font:10px var(--mono);color:var(--muted)}.eventmark{width:6px;height:6px;border-radius:50%;background:#75847d;margin-top:5px}.eventmark.green{background:var(--green)}.eventmark.amber{background:var(--amber)}.eventtext{color:var(--soft)}.eventtext small{display:block;color:var(--muted);font-size:10px;margin-top:2px}.tickstrip{display:flex;gap:6px;flex-wrap:wrap}.tick{border:1px solid var(--line);background:#1b2223;padding:5px 7px;min-width:46px;text-align:center}.tick b{display:block;font:600 14px var(--mono)}.tick small{display:block;color:var(--muted);font:9px var(--mono)}.foot{display:flex;justify-content:space-between;gap:16px;padding:16px 0 0;color:var(--muted);font-size:10px}.mono{font-family:var(--mono)}.hidden{display:none!important}.empty{padding:18px 8px;color:var(--muted);font-size:11px}.right{text-align:right}.positive{color:var(--green)}.negative{color:var(--red)}
.digit-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px}.digit-card{border:1px solid var(--line);background:#151a1b;padding:11px;min-width:0}.digit-card.trade{border-color:#456b55}.digit-card-head{display:flex;align-items:center;justify-content:space-between;gap:8px}.digit-card-number{font:600 24px var(--mono)}.digit-card-prob{font:600 18px var(--mono);color:var(--soft);margin-top:7px}.digit-card-label{color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.08em}.digit-health{font:600 12px var(--mono)}.health-track{height:4px;background:#293233;margin:5px 0 8px}.health-track i{height:100%;display:block;background:var(--amber)}.digit-card.trade .health-track i{background:var(--green)}.digit-card-meta{display:grid;grid-template-columns:1fr 1fr;gap:5px 10px;color:var(--muted);font-size:10px}.digit-card-meta strong{display:block;color:var(--soft);font:11px var(--mono);margin-top:1px}.digit-reason{font-size:10px;color:var(--muted);margin-top:8px;min-height:28px}.health-note{color:var(--muted);font-size:10px;margin:10px 0 0}.scroll-table{overflow:auto;max-height:420px}.compact-table td,.compact-table th{padding:6px 7px}.metric-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.metric-box{border:1px solid var(--line2);padding:10px}.metric-box h3{margin:0 0 7px;font-size:11px}.vol-value{font:600 13px var(--mono)}
@media(max-width:1050px){.lower{grid-template-columns:1fr 1fr}.lower .activity-panel{grid-column:1/-1}.layout-main{grid-template-columns:1fr}.side-stack{grid-template-columns:1fr 1fr;grid-template-rows:auto}.kpis{grid-template-columns:repeat(3,1fr)}.kpi:nth-child(3){border-right:0}.kpi:nth-child(n+4){border-top:1px solid var(--line)}}@media(max-width:680px){.topbar{height:auto;padding:13px 15px;align-items:flex-start;flex-direction:column}.topmeta{width:100%;justify-content:space-between}.wrap{padding:16px 13px 26px}.pagehead{align-items:flex-start;flex-direction:column}.updated{text-align:left}.signal-body{grid-template-columns:90px 1fr;gap:12px}.digit-block{padding-right:12px}.bigdigit{font-size:58px}.signal-details{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.side-stack{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(2,1fr)}.kpi:nth-child(2n){border-right:0}.kpi:nth-child(3){border-right:1px solid var(--line)}.kpi:nth-child(n+3){border-top:1px solid var(--line)}.lower{grid-template-columns:1fr}.lower .activity-panel{grid-column:auto}.foot{flex-direction:column}.toolbar{width:100%;justify-content:space-between}.toolbar select{flex:1}}
</style><style>
@media(max-width:1100px){.digit-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:680px){.digit-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.metric-grid{grid-template-columns:1fr}}
</style></head>
<body>
<header class="topbar"><div class="brand"><div class="brandmark">BX</div><div class="brandname">BROMAX <span>/ Market desk</span></div></div><div class="topmeta"><div class="connection"><i class="dot" id="connDot"></i><span id="connText">Checking market feed</span></div><div class="toolbar"><label for="symbol">Market</label><select id="symbol">__SYMBOL_OPTIONS__</select><button id="refreshBtn" type="button">Refresh</button></div></div></header>
<main class="wrap">
<div class="pagehead"><div><div class="eyebrow">Deriv synthetic indices · MATCH research</div><h1>Market overview</h1><p>Live market readings, strategy evidence and paper execution in one place.</p></div><div class="updated">Last dashboard update<br><strong id="updatedAt">—</strong></div></div>
<div class="statusline"><i class="dot" id="statusDot"></i><span><strong id="systemStatus">Starting up</strong> <span id="systemMessage">Waiting for the first market response.</span></span><span class="tag" style="margin-left:auto">PAPER MODE · REAL ORDERS OFF</span></div>
<section class="layout-main">
  <article class="panel signal-panel"><div class="panelhead"><div><div class="eyebrow">Execution handoff</div><div class="paneltitle" style="margin-top:3px">Ready to trade</div></div><div id="signalState" class="signal-state"><i class="dot warn"></i> WAITING</div></div>
    <div id="signalContent" class="empty-signal"><div class="empty-icon">—</div><div><h3>No approved signal</h3><p>BROMAX is monitoring the selected market. A prediction only appears here when its strategy, quote, timing and paper-risk checks all pass.</p></div></div>
    <div id="signalFooter" class="signal-footer"><span>Signal handoff is inactive until an approved strategy qualifies.</span><code id="signalId">NO SIGNAL</code></div>
  </article>
  <div class="side-stack">
    <article class="panel"><div class="panelhead"><div class="paneltitle">Selected market</div><span class="tag" id="marketTag">—</span></div><div class="marketline"><div><div class="eyebrow">Latest quote</div><div class="price" id="latestPrice">—</div></div><div class="right"><div class="eyebrow">Last digit</div><div class="lastdigit" id="latestDigit">—</div></div></div><div class="microstats"><div><label>Ticks stored</label><strong id="tickCount">—</strong></div><div><label>Last tick time</label><strong id="tickTime">—</strong></div><div><label>Market health</label><strong id="marketHealth">—</strong></div><div><label>Volatility regime</label><strong id="volatilityRegime">—</strong></div></div></article>
    <article class="panel"><div class="panelhead"><div class="paneltitle">Execution controls</div><span class="tag warn">SIMULATED</span></div><div class="guard-row"><span>Execution mode</span><strong>Paper only</strong></div><div class="guard-row"><span>Configured stake</span><strong id="stakeValue">—</strong></div><div class="guard-row"><span>Daily loss limit</span><strong id="lossLimit">—</strong></div><div class="guard-row"><span>Trades today</span><strong id="tradeCount">—</strong></div><div class="guard-row"><span>Live order endpoint</span><strong class="positive">Disabled</strong></div></article>
  </div>
</section>
<section class="panel" style="margin-top:14px"><div class="panelhead"><div><div class="paneltitle">Digit-by-digit intelligence</div><div class="panelnote">Model probabilities are estimates, not guarantees. Health is an evidence-quality score, not a probability.</div></div><span class="tag warn" id="digitDecisionTag">NO QUALIFIED SIGNAL</span></div><div id="digitCards" class="digit-grid"><div class="empty">Waiting for research evidence.</div></div><p class="health-note" id="healthDefinition">No sample values are shown until forecasts and outcomes have been recorded.</p></section>
<section class="lower" style="margin-top:14px;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr)">
  <article class="panel"><div class="panelhead"><div><div class="paneltitle">Per-digit duration analysis</div><div class="panelnote">Observed quote-settled outcomes only · 1–5 ticks</div></div><span class="tag warn">RESEARCH ONLY</span></div><div id="digitDurations" class="scroll-table"><div class="empty">Waiting for quoted paper outcomes.</div></div></article>
  <article class="panel"><div class="panelhead"><div><div class="paneltitle">Paper performance breakdown</div><div class="panelnote">Settled ledger outcomes only</div></div><span class="tag">AUDIT DATA</span></div><div id="performanceBreakdown"><div class="empty">No settled paper outcomes are recorded.</div></div></article>
</section>
<section class="kpis" aria-label="Paper trading performance"><div class="kpi"><label>Paper trades opened</label><strong id="kpiOpened">—</strong><small>Recorded contracts</small></div><div class="kpi"><label>Settled</label><strong id="kpiSettled">—</strong><small id="kpiPending">— pending</small></div><div class="kpi"><label>Win rate</label><strong id="kpiWinRate">—</strong><small>Settled contracts only</small></div><div class="kpi"><label>Net paper P/L</label><strong id="kpiPnl">—</strong><small>After settled outcomes</small></div><div class="kpi"><label>Latest digit frequency</label><strong id="kpiFreq">—</strong><small>Selected rolling window</small></div></section>
<section class="lower">
  <article class="panel"><div class="panelhead"><div><div class="paneltitle">Digit distribution</div><div class="panelnote">Last <span id="distWindow">1,000</span> ticks · 10% reference</div></div><span class="tag" id="distCount">0 ticks</span></div><div id="distribution" class="empty">Waiting for enough tick history.</div><div class="panelnote" style="margin-top:12px">Observed frequency is descriptive; it does not establish that a digit is more likely on the next tick.</div></article>
  <article class="panel"><div class="panelhead"><div><div class="paneltitle">Strategy monitor</div><div class="panelnote">Current candidate forecasts and validation state</div></div><span class="tag">RESEARCH</span></div><div id="strategies" class="tablewrap"><div class="empty">Waiting for strategy output.</div></div></article>
  <article class="panel activity-panel"><div class="panelhead"><div class="paneltitle">Recent activity</div><span class="panelnote">Newest first</span></div><div id="activity" class="activity"><div class="empty">No events recorded yet.</div></div></article>
</section>
<section class="panel" style="margin-top:14px"><div class="panelhead"><div><div class="paneltitle">Timing & volatility research</div><div class="panelnote">1–5 tick horizons · trailing volatility regimes · research only</div></div><span class="tag warn" id="timingTag">NOT VALIDATED</span></div><div id="timingResearch" class="empty">Collecting forecast history for multi-tick analysis.</div><div class="paneltitle" style="margin-top:18px">Actual payout & expected-profit test</div><div class="panelnote">Prospective Deriv proposal quotes matched to their exact future settlement tick</div><div id="payoutTiming" class="empty">Waiting for duration-specific quote and settlement pairs.</div><p class="panelnote" style="margin:10px 0 0">A suggested duration is not an executable setting until duration-specific Deriv quotes, untouched out-of-sample results and forward paper settlements confirm positive expected value. Volatility describes market conditions; it cannot prove a digit.</p></section>
<section class="panel" style="margin-top:14px"><div class="panelhead"><div><div class="paneltitle">Latest ticks</div><div class="panelnote">Most recent observations received from Deriv</div></div><span class="tag" id="tickFeedTag">NO FEED</span></div><div id="tickTape" class="tickstrip"><div class="empty">Waiting for live ticks.</div></div></section>
<section class="lower" style="margin-top:14px;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr)">
  <article class="panel"><div class="panelhead"><div><div class="paneltitle">Paper execution history</div><div class="panelnote">Only simulated contracts are shown here</div></div><span class="tag">AUDIT LOG</span></div><div id="trades" class="tablewrap"><div class="empty">No paper contracts recorded.</div></div></article>
  <article class="panel"><div class="panelhead"><div><div class="paneltitle">Research status</div><div class="panelnote">Evidence required before a strategy can be enabled</div></div></div><div id="research" class="empty">Waiting for research report.</div></article>
</section>
<div class="foot"><span>BROMAX · Quantitative research and paper execution</span><span>Signals are not guarantees. No strategy is assumed profitable. Real-money trading is not enabled.</span></div>
</main>
<script>
'use strict';
const $=id=>document.getElementById(id);const state={symbol:$('symbol').value,last:null};
const money=v=>v==null?'—':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(Number(v));
const pct=v=>v==null?'—':(Number(v)*100).toFixed(1)+'%';const nfmt=v=>v==null?'—':Number(v).toLocaleString('en-US');
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path){const r=await fetch(path,{cache:'no-store'});if(!r.ok)throw new Error(`${r.status} ${r.statusText}`);return r.json()}
function setText(id,value){$(id).textContent=value??'—'}function fmtTime(epoch){if(!epoch)return'—';return new Date(Number(epoch)*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function eventTime(value){if(!value)return'—';const d=new Date(value.endsWith('Z')?value:value+'Z');return isNaN(d)?'—':d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function renderDistribution(d){const counts=d.counts||{};const total=Number(d.n||0);$('distCount').textContent=`${nfmt(total)} ticks`;$('distWindow').textContent=nfmt(d.window||1000);if(!total){$('distribution').innerHTML='<div class="empty">No digit observations have been collected yet.</div>';return}const max=Math.max(1,...Object.values(counts).map(Number));$('distribution').innerHTML='<div class="digitrows">'+Array.from({length:10},(_,i)=>{const c=Number(counts[i]??counts[String(i)]??0);const p=c/total;return `<div class="digitrow"><span class="n">${i}</span><div class="track"><i style="width:${Math.min(100,c/max*100)}%"></i></div><span class="pct">${(p*100).toFixed(1)}%</span></div>`}).join('')+'</div>';$('kpiFreq').textContent=pct(Math.max(...Array.from({length:10},(_,i)=>Number(d.frequencies?.[i]??d.frequencies?.[String(i)]??0))))}
function renderDigitAnalysis(data){const digits=data?.digits||[];if(digits.length!==10){$('digitCards').innerHTML='<div class="empty">Waiting for complete digit analysis.</div>';return}const candidates=digits.filter(x=>x.decision==='PAPER TRADE CANDIDATE');$('digitDecisionTag').className='tag '+(candidates.length?'good':'warn');$('digitDecisionTag').textContent=candidates.length?`${candidates.length} PAPER CANDIDATE${candidates.length===1?'':'S'}`:'NO QUALIFIED SIGNAL';$('healthDefinition').textContent=data.health_definition||data.note||'Health is an evidence-quality measure, not a probability.';setText('marketHealth',`${nfmt(data.market_health_percent)}%`);setText('volatilityRegime',data.volatility?.regime||'INSUFFICIENT DATA');$('digitCards').innerHTML=digits.map(x=>{const ci=x.confidence_interval_95;const trade=x.decision==='PAPER TRADE CANDIDATE';const reason=x.reasons?.[0]||x.durations?.['1']?.reasons?.[0]||'No per-digit evidence is available.';const health=Math.max(0,Math.min(100,Number(x.health_percent||0)));return `<article class="digit-card ${trade?'trade':''}"><div class="digit-card-head"><span class="digit-card-number">${esc(x.digit)}</span><span class="tag ${trade?'good':'warn'}">${trade?'PAPER TRADE':'DO NOT TRADE'}</span></div><div class="digit-card-label" style="margin-top:7px">Estimated match probability</div><div class="digit-card-prob">${pct(x.estimated_win_probability)}</div><div class="digit-card-head" style="margin-top:8px"><span class="digit-card-label">Evidence health</span><span class="digit-health">${health}%</span></div><div class="health-track"><i style="width:${health}%"></i></div><div class="digit-card-meta"><div>Observed rate<strong>${pct(x.historical_observed_match_rate)}</strong></div><div>95% CI<strong>${ci?`${pct(ci[0])}–${pct(ci[1])}`:'—'}</strong></div><div>Scored samples<strong>${nfmt(x.sample_size)}</strong></div><div>Quoted samples<strong>${nfmt(x.quoted_sample_size)}</strong></div><div>Last 1,000 frequency<strong>${pct(x.current_frequency)}</strong></div><div>Recent 100 frequency<strong>${pct(x.recent_frequency)}</strong></div></div><div class="digit-reason" title="${esc((x.reasons||[]).join(' '))}">${esc(reason)}</div></article>`}).join('');const rows=[];for(const digit of digits){for(const duration of [1,2,3,4,5]){const e=digit.durations?.[String(duration)];const hist=e?.historical||{};const quote=e?.current_quote||{};rows.push(`<tr><td class="num">${digit.digit}</td><td class="num">${duration}t</td><td class="num">${nfmt(hist.n||0)}</td><td class="num">${pct(hist.hit_rate)}</td><td class="num">${money(hist.net_pnl)}</td><td class="num">${quote.available?`${money(quote.payout)} / EV ${money(quote.expected_value)}`:'—'}</td><td><span class="tag warn">${esc(hist.out_of_sample_status||'RESEARCH_ONLY')}</span></td></tr>`)}}$('digitDurations').innerHTML=`<table class="compact-table"><thead><tr><th>Digit</th><th>Duration</th><th>Settled</th><th>Observed hit</th><th>Net paper P/L</th><th>Current quote</th><th>Evidence</th></tr></thead><tbody>${rows.join('')}</tbody></table>`;const perf=data.paper_performance||{};const groups=[['By digit',perf.by_digit||{}],['By strategy',perf.by_strategy||{}]];$('performanceBreakdown').innerHTML=perf.settled_contracts?groups.map(([label,items])=>`<h3 class="paneltitle" style="margin:10px 0 5px">${label}</h3><div class="tablewrap"><table class="compact-table"><thead><tr><th>Group</th><th>Contracts</th><th>Wins</th><th>Win rate</th><th>Net P/L</th></tr></thead><tbody>${Object.entries(items).map(([name,v])=>`<tr><td>${esc(name)}</td><td class="num">${nfmt(v.contracts)}</td><td class="num">${nfmt(v.wins)}</td><td class="num">${pct(v.win_rate)}</td><td class="num">${money(v.net_pnl)}</td></tr>`).join('')}</tbody></table></div>`).join(''):'<div class="empty">No settled paper outcomes are recorded.</div>'}
function renderSignal(ready,signals,symbol){const sig=ready?.signal||ready?.ready_signal||null;const reason=ready?.reason||ready?.status_reason||'No strategy currently meets every approval and execution condition.';const stateName=ready?.status||'WAITING';const approved=Boolean(sig&&ready?.eligible===true);$('signalState').className='signal-state '+(approved?'ready':stateName==='REJECTED'?'rejected':'');$('signalState').innerHTML=`<i class="dot ${approved?'ok':stateName==='REJECTED'?'warn':''}"></i> ${esc(approved?'READY':stateName)}`;
 if(!approved){$('signalContent').className='empty-signal';$('signalContent').innerHTML=`<div class="empty-icon">${stateName==='REJECTED'?'!':'—'}</div><div><h3>${stateName==='REJECTED'?'Signal not approved':'No approved signal'}</h3><p>${esc(reason)}</p></div>`;$('signalFooter').innerHTML=`<span>Candidate forecasts remain research-only until every gate passes.</span><code id="signalId">${esc(ready?.signal_id||'NO SIGNAL')}</code>`;return}
 $('signalContent').className='signal-body';$('signalContent').innerHTML=`<div class="digit-block"><div class="digit-caption">MATCH digit</div><div class="bigdigit ready">${esc(sig.predicted_digit)}</div><div class="digit-sub">${esc(sig.strategy_id||'Approved strategy')}</div></div><div class="signal-details"><div class="detail"><label>Market</label><strong>${esc(sig.symbol||symbol)}</strong></div><div class="detail"><label>Contract</label><strong>DIGITMATCH</strong></div><div class="detail"><label>Duration</label><strong>${esc(sig.duration||1)} tick</strong></div><div class="detail"><label>Paper stake</label><strong>${money(sig.stake)}</strong></div><div class="detail"><label>Win probability</label><strong>${pct(sig.probability)}</strong></div><div class="detail"><label>Quoted payout</label><strong>${money(sig.payout)}</strong></div><div class="detail"><label>Break-even</label><strong>${pct(sig.break_even_probability)}</strong></div><div class="detail"><label>Expected value</label><strong>${money(sig.expected_value)}</strong></div><div class="detail"><label>Signal expires</label><strong>${fmtTime(sig.expires_epoch)}</strong></div></div>`;
 $('signalFooter').innerHTML=`<span>Approved paper signal · exact forecast passed to simulator</span><code>${esc(sig.signal_id||'—')}</code>`}
function renderStrategies(data,ready,analysis){const list=analysis?.strategies||[];if(!list.length){$('strategies').innerHTML='<div class="empty">No forecasts yet. The collector needs a full history window before strategies can be evaluated.</div>';return}$('strategies').innerHTML=`<table><thead><tr><th>Strategy</th><th>Digit</th><th>Model p</th><th>Validation n</th><th>Hit rate</th><th>Brier</th><th>Status</th></tr></thead><tbody>${list.map(x=>{const approved=x.explicitly_approved&&x.final_test_status==='evidence_of_edge';return `<tr><td class="strategy-name">${esc(x.strategy_id)}${x.selected?' · selected':''}</td><td class="num">${esc(x.predicted_digit)}</td><td class="num">${pct(x.predicted_digit_probability)}</td><td class="num">${nfmt(x.validation_sample_size)}</td><td class="num">${pct(x.validation_hit_rate)}</td><td class="num">${x.validation_brier==null?'—':Number(x.validation_brier).toFixed(4)}</td><td><span class="tag ${approved?'good':'warn'}" title="${esc(JSON.stringify(x.validation_calibration||[]))}">${approved?'APPROVED':'RESEARCH ONLY'}</span></td></tr>`}).join('')}</tbody></table><div class="panelnote" style="padding:9px 8px 0">Model probability is not a validated win rate. Calibration details are available on each status badge. Ranking alone never authorizes execution.</div>`}
function renderActivity(trades,signals,health){const ev=[];for(const t of (trades||[]).slice(-8)){ev.push({time:t.created_utc,type:t.event_type==='PAPER_OPEN'?'Paper contract opened':'Paper contract settled',detail:`${t.symbol} · digit ${t.prediction??'—'}${t.paper_pnl!=null?' · P/L '+money(t.paper_pnl):''}`,tone:t.event_type==='PAPER_SETTLE'?'green':'amber'})}for(const e of (health?.recent_errors||[]).slice(-3)){ev.push({time:null,type:'Feed / service notice',detail:e,tone:'amber'})}ev.sort((a,b)=>new Date(b.time||0)-new Date(a.time||0));$('activity').innerHTML=ev.length?ev.slice(0,7).map(e=>`<div class="event"><time>${eventTime(e.time)}</time><i class="eventmark ${e.tone}"></i><div class="eventtext">${esc(e.type)}<small>${esc(e.detail)}</small></div></div>`).join(''):'<div class="empty">No recent events. New forecasts and paper executions will appear here.</div>'}
function renderTrades(rows){const opens=(rows||[]).filter(x=>x.event_type==='PAPER_OPEN').slice(-8).reverse();if(!opens.length){$('trades').innerHTML='<div class="empty">No paper contracts have been opened. This is normal while strategies remain unapproved.</div>';return}$('trades').innerHTML=`<table><thead><tr><th>Time</th><th>Market</th><th>Digit</th><th>Stake</th><th>Payout</th><th>Outcome</th></tr></thead><tbody>${opens.map(x=>{const settle=(rows||[]).find(y=>y.event_type==='PAPER_SETTLE'&&y.note===String(x.seq));return `<tr><td class="num">${eventTime(x.created_utc)}</td><td>${esc(x.symbol)}</td><td class="num">${esc(x.prediction)}</td><td class="num">${money(x.stake)}</td><td class="num">${money(x.payout)}</td><td>${settle?`<span class="${Number(settle.paper_pnl)>=0?'positive':'negative'}">${Number(settle.hit)?'WIN':'LOSS'} · ${money(settle.paper_pnl)}</span>`:'<span class="muted">OPEN</span>'}</td></tr>`}).join('')}</tbody></table>`}
function renderPayoutTiming(data){const root=$('payoutTiming');if(!data){root.innerHTML='<div class="empty">Payout test is not available.</div>';return}const best=data.best_candidate;const rows=data.strategies||[];if(!rows.length){root.innerHTML=`<div class="guard-row"><span>Quote-settled pairs</span><strong>${nfmt(data.quote_settled_pairs||0)}</strong></div><div class="empty">${esc(data.note||'Collecting live Deriv proposal quotes and waiting for each duration to settle. Historical ticks cannot recreate past payouts.')}</div>`;return}root.innerHTML=`<div class="guard-row"><span>Quote-settled contracts</span><strong>${nfmt(data.quote_settled_pairs)}</strong></div><div class="guard-row"><span>Best statistically supported duration</span><strong>${best?`${esc(best.duration_ticks)} ticks · ${esc(best.strategy_id)}`:'NONE — evidence insufficient'}</strong></div><div class="guard-row"><span>Execution authorization</span><strong>DISABLED</strong></div><div class="tablewrap"><table><thead><tr><th>Strategy</th><th>Ticks</th><th>Settled</th><th>Net P/L</th><th>Mean return</th><th>Final 95% CI</th><th>Decision</th></tr></thead><tbody>${rows.map(x=>{const f=x.final_test;return `<tr><td>${esc(x.strategy_id)}</td><td class="num">${x.duration_ticks}t</td><td class="num">${nfmt(f?.n||0)}</td><td class="num ${Number(f?.total_pnl||0)>=0?'positive':'negative'}">${money(f?.total_pnl)}</td><td class="num">${pct(f?.mean_return_on_stake)}</td><td class="num">${f?.mean_return_ci95?`${pct(f.mean_return_ci95[0])} to ${pct(f.mean_return_ci95[1])}`:'—'}</td><td><span class="tag ${x.payout_ev_status==='OUT_OF_SAMPLE_PROFIT_EVIDENCE'?'good':'warn'}">${esc(x.payout_ev_status)}</span></td></tr>`}).join('')}</tbody></table></div><p class="panelnote" style="margin-top:10px">Net P/L uses the actual quoted stake and gross payout for each settled contract. Mean return confidence intervals and Holm-adjusted significance are calculated on chronological holdouts. A positive result is evidence, not a promise.</p>`}
function renderTiming(data){const root=$('timingResearch');const tag=$('timingTag');if(!data||!Array.isArray(data.strategies)||!data.strategies.length){tag.textContent='INSUFFICIENT DATA';root.innerHTML='<div class="empty">Not enough scored forecast history to compare digit hit rates yet.</div>';renderPayoutTiming(data?.payout_optimization);return}const best=data.best_research_candidate;tag.textContent=best?'RESEARCH LEAD · NOT APPROVED':'NO VALIDATED TIMING';root.innerHTML=`<div class="guard-row"><span>Current volatility regime</span><strong>${esc(data.current_volatility_regime||'INSUFFICIENT DATA')}</strong></div><div class="guard-row"><span>Best hit-rate research candidate</span><strong>${best?`${esc(best.duration_ticks)} ticks · ${esc(best.strategy_id)}`:'None'}</strong></div><div class="tablewrap"><table><thead><tr><th>Strategy</th><th>Duration</th><th>Samples</th><th>Hit rate</th><th>95% CI lower</th><th>Status</th></tr></thead><tbody>${data.strategies.map(x=>`<tr><td>${esc(x.strategy_id)}</td><td class="num">${esc(x.duration_ticks)}t</td><td class="num">${nfmt(x.n)}</td><td class="num">${pct(x.hit_rate)}</td><td class="num">${x.hit_rate_ci95?pct(x.hit_rate_ci95[0]):'—'}</td><td><span class="tag warn">${esc(x.timing_status)}</span></td></tr>`).join('')}</tbody></table></div><div class="guard-row"><span>Execution authorization</span><strong>DISABLED</strong></div>`;renderPayoutTiming(data.payout_optimization)}
function renderResearch(report,symbol){const item=(report?.symbols||[]).find(x=>x.symbol===symbol);if(!item){$('research').textContent='No research report is available for this market yet.';return}const finalists=item.validation_finalists||[];$('research').innerHTML=`<div class="guard-row"><span>Research status</span><strong>${esc(item.status||'In progress')}</strong></div><div class="guard-row"><span>Ticks analyzed</span><strong>${nfmt(item.n_ticks)}</strong></div><div class="guard-row"><span>Validation finalists</span><strong>${finalists.length?nfmt(finalists.length):'None'}</strong></div><div class="guard-row"><span>Final test accessed</span><strong>${item.final_test_accessed?'Yes':'No'}</strong></div><p class="panelnote" style="margin:10px 0 0">A strategy requires frozen out-of-sample evidence and explicit approval before it can generate an execution-ready paper signal.</p>`}
async function refreshAll(){const symbol=state.symbol;try{const [h,t,d,p,ins,sg,ready,rep,tr,timing,da]=await Promise.all([api('/health'),api(`/ticks/latest?symbol=${encodeURIComponent(symbol)}&limit=12`),api(`/digits/distribution?symbol=${encodeURIComponent(symbol)}&window=1000`),api('/paper/status'),api(`/intelligence?symbol=${encodeURIComponent(symbol)}&window=1000`),api(`/signals?symbol=${encodeURIComponent(symbol)}`),api(`/signals/ready?symbol=${encodeURIComponent(symbol)}`),api('/report'),api(`/paper/trades?symbol=${encodeURIComponent(symbol)}`),api(`/timing?symbol=${encodeURIComponent(symbol)}`),api(`/digits/analysis?symbol=${encodeURIComponent(symbol)}`)]);
 const stale=Boolean(h.stale?.[symbol]);$('connDot').className='dot '+(!stale?'ok':'warn');setText('connText',stale?'Market feed delayed':'Market feed connected');$('statusDot').className='dot '+(!stale?'ok':'warn');setText('systemStatus',stale?'Feed unavailable or stale':'Market feed operating');setText('systemMessage',stale?'No recent tick received for this symbol. New signals are suspended.':'Analysis and execution checks are running in paper mode.');setText('updatedAt',new Date().toLocaleTimeString());setText('tickCount',nfmt(h.tick_counts?.[symbol]||0));setText('marketTag',symbol);setText('tickFeedTag',t.length?'LIVE TICKS':'NO FEED');if(t.length){const x=t[0];setText('latestPrice',x.quote_text);setText('latestDigit',x.digit);setText('tickTime',fmtTime(x.epoch));$('tickTape').innerHTML=t.slice(0,12).map(x=>`<div class="tick"><b>${esc(x.digit)}</b><small>${fmtTime(x.epoch)}</small></div>`).join('')}else{$('latestPrice').textContent='—';$('latestDigit').textContent='—';$('tickTime').textContent='—';$('tickTape').innerHTML='<div class="empty">No ticks received yet.</div>'}
 renderDistribution({...d,window:1000});renderDigitAnalysis(da);renderSignal(ready,sg,symbol);renderStrategies(sg,ready,da);setText('stakeValue',money(p.stake));setText('lossLimit',money(p.daily_loss_cap));setText('tradeCount',`${nfmt(p.settled_total||0)} settled / ${nfmt(p.max_trades_per_day||0)} limit`);setText('kpiOpened',nfmt(p.opened_total||0));setText('kpiSettled',nfmt(p.settled_total||0));setText('kpiPending',`${nfmt(p.pending||0)} pending`);const settled=tr.filter(x=>x.event_type==='PAPER_SETTLE');const wins=settled.filter(x=>Number(x.hit)===1).length;setText('kpiWinRate',settled.length?pct(wins/settled.length):'—');const pnl=settled.reduce((a,x)=>a+Number(x.paper_pnl||0),0);setText('kpiPnl',settled.length?money(pnl):'—');$('kpiPnl').className=settled.length?(pnl>=0?'positive':'negative'):'';renderTrades(tr);renderActivity(tr,sg,h);renderResearch(rep,symbol);renderTiming(timing);
 }catch(e){$('connDot').className='dot warn';setText('connText','Dashboard API unavailable');setText('systemStatus','Unable to refresh');setText('systemMessage',e.message)}}
$('symbol').addEventListener('change',e=>{state.symbol=e.target.value;refreshAll()});$('refreshBtn').addEventListener('click',refreshAll);refreshAll();setInterval(refreshAll,3000);
</script></body></html>
```

## `bromax/dashboard_minimal.html`

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BROMAX — Decision Terminal</title>
<style>
:root{color-scheme:dark;--bg:#0d1215;--panel:#151d21;--panel2:#1a2429;--line:#2b383d;--text:#e7efec;--muted:#91a19d;--green:#73d39a;--red:#f07c7c;--amber:#e4bd70;--blue:#8eb8c9;--mono:ui-monospace,Consolas,monospace;--sans:Inter,"Segoe UI",Arial,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.35 var(--sans);font-variant-numeric:tabular-nums}button,select{font:inherit;color:inherit;background:var(--panel2);border:1px solid var(--line);border-radius:5px;padding:7px 10px}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}.topbar{height:48px;display:flex;align-items:center;justify-content:space-between;padding:0 20px;border-bottom:1px solid var(--line);background:#11181b}.brand{font-weight:750;letter-spacing:.13em}.brand small{color:var(--muted);font-weight:500;letter-spacing:.02em}.tools{display:flex;align-items:center;gap:14px}.tools select{min-width:145px}.shell{max-width:1320px;margin:0 auto;padding:13px 18px 18px;display:grid;gap:11px}.section{border:1px solid var(--line);border-radius:7px;background:var(--panel);padding:11px 13px;min-width:0}.section-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}.section-title h2{margin:0;font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-weight:700}.market{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.market-item{min-width:0}.label{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}.value{font:600 15px var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.status{display:inline-flex;align-items:center;gap:7px;font:700 11px var(--mono);letter-spacing:.04em}.light{width:8px;height:8px;border-radius:50%;background:#69757a}.status.good{color:var(--green)}.status.good .light{background:var(--green)}.status.bad{color:var(--red)}.status.bad .light{background:var(--red)}.status.warn{color:var(--amber)}.status.warn .light{background:var(--amber)}.signal{border-color:#496b58;background:linear-gradient(110deg,#18251f,#151d21 62%);padding:13px 15px}.signal .section-title{margin-bottom:10px}.signal .section-title h2{color:#b4c6bc}.signal-grid{display:grid;grid-template-columns:1.15fr repeat(5,minmax(100px,.7fr));gap:10px;align-items:center}.signal-digit{font:700 56px/.9 var(--mono);color:var(--green)}.signal-digit.empty{font:700 20px var(--sans);color:var(--red);letter-spacing:.02em;line-height:1.2}.signal-item .value{font-size:16px}.trade-pill{display:inline-block;padding:6px 11px;border:1px solid var(--line);border-radius:4px;font:700 12px var(--mono);letter-spacing:.06em;white-space:nowrap}.trade-pill.trade{color:var(--green);border-color:#4d8c65;background:#173122}.trade-pill.no-trade{color:var(--red);border-color:#754949;background:#301c1d}.digit-wrap{overflow:auto;max-height:335px}table{width:100%;border-collapse:collapse;min-width:720px}th{text-align:left;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:650;padding:7px 9px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}td{padding:6px 9px;border-bottom:1px solid #222d31;font-size:12px;vertical-align:middle}tbody tr:last-child td{border-bottom:0}td.num{font-family:var(--mono);white-space:nowrap}.digit-cell{font:700 14px var(--mono)}.row-trade{background:#14251c}.row-trade .digit-cell,.decision-trade{color:var(--green);font-weight:750}.decision-no{color:var(--red);font-weight:700}.reason{color:var(--muted);font-size:11px;max-width:540px;white-space:normal}.bottom{display:grid;grid-template-columns:1fr 1.4fr;gap:11px}.summary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px 12px}.summary-grid .value{font-size:14px}.muted{color:var(--muted)}.good-text{color:var(--green)}.bad-text{color:var(--red)}.warn-text{color:var(--amber)}.note{color:var(--muted);font-size:10px;margin:8px 0 0}.updated{color:var(--muted);font-size:10px;text-align:right}.error{color:var(--red)}.ops-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.ops-grid .value{font-size:13px}.history-wrap{overflow:auto;max-height:230px;margin-top:8px}.history-table{min-width:600px}.last-error{margin:8px 0 0;color:var(--red);overflow-wrap:anywhere}@media(max-width:850px){.signal-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.signal-primary{grid-column:span 3}.bottom{grid-template-columns:1fr}.shell{padding:10px}.market{grid-template-columns:repeat(2,minmax(0,1fr))}.ops-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:520px){.topbar{padding:0 10px}.brand small{display:none}.tools{gap:7px}.tools select{min-width:0;width:118px}.shell{gap:8px;padding:8px}.section{padding:9px}.market{gap:8px}.signal-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.signal-primary{grid-column:span 2}.signal-digit{font-size:44px}.signal-item .value{font-size:14px}.summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.ops-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.updated{display:none}}
</style>
</head>
<body>
<header class="topbar"><div class="brand">BROMAX <small> / DECISION TERMINAL · PAPER ONLY</small></div><div class="tools"><select id="symbol" aria-label="Market symbol">__SYMBOL_OPTIONS__</select><a href="/advanced">Advanced analysis</a></div></header>
<main class="shell">
  <section class="section" aria-labelledby="market-heading">
    <div class="section-title"><h2 id="market-heading">Market status</h2><span class="updated" id="updatedAt">Waiting for data</span></div>
    <div class="market">
      <div class="market-item"><span class="label">Deriv symbol</span><strong class="value" id="marketSymbol">—</strong></div>
      <div class="market-item"><span class="label">Connection</span><strong class="status" id="connection"><i class="light"></i><span>CONNECTING</span></strong></div>
      <div class="market-item"><span class="label">Latest tick · digit · market time</span><strong class="value" id="latestTick">—</strong></div>
      <div class="market-item"><span class="label">Data freshness</span><strong class="value" id="freshness">—</strong></div>
    </div>
  </section>

  <section class="section" aria-labelledby="pipeline-heading">
    <div class="section-title"><h2 id="pipeline-heading">Live analysis pipeline</h2><strong class="status" id="pipelineStatus"><i class="light"></i><span>WAITING</span></strong></div>
    <div class="ops-grid">
      <div><span class="label">Subscription</span><strong class="value" id="subscriptionStatus">—</strong></div>
      <div><span class="label">Analysis engine</span><strong class="value" id="analysisStatus">—</strong></div>
      <div><span class="label">Strategy processing</span><strong class="value" id="strategyStatus">—</strong></div>
      <div><span class="label">Signal generation</span><strong class="value" id="generationStatus">—</strong></div>
      <div><span class="label">Ticks received / processed</span><strong class="value" id="tickCounts">—</strong></div>
      <div><span class="label">Signals recorded</span><strong class="value" id="signalsRecorded">—</strong></div>
      <div><span class="label">Missed tick epochs</span><strong class="value" id="missingTicks">—</strong></div>
      <div><span class="label">Out-of-order / invalid / duplicate</span><strong class="value" id="badTicks">—</strong></div>
    </div>
    <p class="last-error" id="pipelineError" hidden></p>
    <div class="history-wrap"><table class="history-table">
      <thead><tr><th>Time</th><th>Strategy</th><th>Digit</th><th>Probability</th><th>Signal status</th></tr></thead>
      <tbody id="signalHistory"><tr><td colspan="5" class="muted">No persisted strategy forecasts yet.</td></tr></tbody>
    </table></div>
    <p class="note">Forecasts are analysis records, not trade instructions. No order is placed.</p>
  </section>

  <section class="section signal" aria-labelledby="signal-heading">
    <div class="section-title"><h2 id="signal-heading">Best trading signal</h2><span id="signalStatus" class="trade-pill no-trade">NO QUALIFIED SIGNAL</span></div>
    <div class="signal-grid">
      <div class="signal-primary"><span class="label">Recommended digit</span><div class="signal-digit empty" id="signalDigit">NO QUALIFIED SIGNAL</div></div>
      <div class="signal-item"><span class="label">Win probability</span><strong class="value" id="signalProbability">—</strong></div>
      <div class="signal-item"><span class="label">Health</span><strong class="value" id="signalHealth">—</strong></div>
      <div class="signal-item"><span class="label">Duration</span><strong class="value" id="signalDuration">—</strong></div>
      <div class="signal-item"><span class="label">Quoted payout</span><strong class="value" id="signalPayout">—</strong></div>
      <div class="signal-item"><span class="label">Expected value</span><strong class="value" id="signalEv">—</strong></div>
    </div>
  </section>

  <section class="section" aria-labelledby="digits-heading">
    <div class="section-title"><h2 id="digits-heading">Digit prediction · all ten digits</h2><span class="muted" id="decisionNote">Estimates are shown only when calculated.</span></div>
    <div class="digit-wrap"><table>
      <thead><tr><th>Digit</th><th>Win probability</th><th>Evidence health</th><th>Decision</th><th>Rejection reason</th></tr></thead>
      <tbody id="digitRows"><tr><td colspan="5" class="muted">Waiting for research data.</td></tr></tbody>
    </table></div>
  </section>

  <div class="bottom">
    <section class="section" aria-labelledby="volatility-heading">
      <div class="section-title"><h2 id="volatility-heading">Volatility check</h2></div>
      <div class="summary-grid">
        <div><span class="label">Current condition</span><strong class="value" id="volatilityCondition">—</strong></div>
        <div><span class="label">Volatility health</span><strong class="value" id="volatilityHealth">—</strong></div>
        <div><span class="label">Signal filter</span><strong class="value" id="volatilityApproval">—</strong></div>
      </div>
    </section>
    <section class="section" aria-labelledby="paper-heading">
      <div class="section-title"><h2 id="paper-heading">Paper trading results</h2></div>
      <div class="summary-grid">
        <div><span class="label">Total trades</span><strong class="value" id="tradeTotal">0</strong></div>
        <div><span class="label">Wins</span><strong class="value" id="tradeWins">0</strong></div>
        <div><span class="label">Losses</span><strong class="value" id="tradeLosses">0</strong></div>
        <div><span class="label">Win rate</span><strong class="value" id="tradeWinRate">—</strong></div>
        <div><span class="label">Net paper profit</span><strong class="value" id="tradeProfit">—</strong></div>
      </div>
    </section>
  </div>
</main>
<script>
'use strict';
const $=id=>document.getElementById(id);
const state={symbol:$('symbol').value,currency:'USD'};
const pct=value=>value==null||!Number.isFinite(Number(value))?'—':`${(Number(value)*100).toFixed(1)}%`;
const score=value=>value==null||!Number.isFinite(Number(value))?'—':`${Number(value)}%`;
const money=value=>value==null||!Number.isFinite(Number(value))?'—':new Intl.NumberFormat('en-US',{style:'currency',currency:state.currency,maximumFractionDigits:2}).format(Number(value));
const safe=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
async function api(path){const response=await fetch(path,{cache:'no-store'});if(!response.ok)throw new Error(`${response.status} ${response.statusText}`);return response.json()}
function renderMarket(decision){
  const symbol=decision.symbol,market=decision.market_status||{},pipeline=decision.pipeline||{},tick=market.latest_tick;
  state.currency=decision.currency||'USD';
  $('marketSymbol').textContent=symbol;
  const receiving=market.data_status==='RECEIVING DATA';
  const statusText=market.connection_status||'DISCONNECTED';
  const connection=$('connection');
  connection.className=`status ${statusText==='CONNECTED'?(receiving?'good':'warn'):statusText==='CONNECTING'||statusText==='RECONNECTING'?'warn':'bad'}`;
  connection.querySelector('span').textContent=statusText;
  if(tick){
    const age=market.freshness_seconds;
    const tickTime=tick.epoch?new Date(Number(tick.epoch)*1000).toLocaleTimeString():'—';
    $('latestTick').textContent=`${tick.quote_text} · ${tick.digit} · ${tickTime}`;
    $('freshness').textContent=`${market.data_status||'STALE'} · ${age==null?'—':`${Math.floor(age)}s`}`;
    $('freshness').className=`value ${receiving?'good-text':'bad-text'}`;
  }else{
    $('latestTick').textContent='—';$('freshness').textContent='NO TICK DATA';$('freshness').className='value bad-text';
  }
  const pipelineStatus=$('pipelineStatus');
  const pipelineText=pipeline.pipeline_status||'WAITING';
  const good=pipelineText==='GENERATING SIGNALS'||pipelineText==='ANALYZING'||pipelineText==='RECEIVING DATA';
  const warn=pipelineText==='CONNECTED'||pipelineText==='CONNECTING';
  pipelineStatus.className=`status ${good?'good':warn?'warn':'bad'}`;
  pipelineStatus.querySelector('span').textContent=pipelineText;
  $('subscriptionStatus').textContent=pipeline.subscription_status||'—';
  $('analysisStatus').textContent=pipeline.analysis_status||'—';
  $('strategyStatus').textContent=pipeline.strategy_status||'—';
  $('generationStatus').textContent=pipeline.signal_generation_status||'—';
  $('tickCounts').textContent=`${pipeline.received_ticks??0} / ${pipeline.processed_ticks??0}`;
  $('signalsRecorded').textContent=String(pipeline.signals_generated??0);
  $('missingTicks').textContent=String(pipeline.missing_tick_count??0);
  $('badTicks').textContent=`${pipeline.out_of_order_ticks??0} / ${pipeline.invalid_ticks??0} / ${pipeline.duplicate_ticks??0}`;
  const error=$('pipelineError');
  error.textContent=pipeline.last_error||pipeline.connection_error||(pipeline.recent_errors||[]).join(' · ');
  error.hidden=!error.textContent;
}
function renderSignalHistory(data){
  const rows=data?.signals||[];
  $('signalHistory').innerHTML=rows.length?rows.slice().reverse().map(row=>{
    const time=row.created_at?new Date(row.created_at).toLocaleTimeString():'—';
    const confidence=row.confidence==null?'—':pct(row.confidence);
    return `<tr><td class="num">${safe(time)}</td><td>${safe(row.strategy_id)}</td><td class="num">${safe(row.prediction)}</td><td class="num">${safe(confidence)}</td><td>${safe(row.status)}</td></tr>`;
  }).join(''):'<tr><td colspan="5" class="muted">No persisted strategy forecasts yet.</td></tr>';
}
function renderSignal(decision){
  const signal=decision?.trade_authorized===true?decision.signal:null;
  const status=$('signalStatus'),hero=$('signalDigit');
  if(!signal){
    status.className='trade-pill no-trade';status.textContent='NO TRADE';
    hero.className='signal-digit empty';hero.textContent='NO QUALIFIED SIGNAL';
    for(const id of ['signalProbability','signalHealth','signalDuration','signalPayout','signalEv'])$(id).textContent='—';
    return;
  }
  status.className='trade-pill trade';status.textContent='TRADE · PAPER ONLY';
  hero.className='signal-digit';hero.textContent=String(signal.predicted_digit);
  $('signalProbability').textContent=pct(signal.probability);
  const digit=decision.digits.find(row=>Number(row.digit)===Number(signal.predicted_digit));
  $('signalHealth').textContent=score(digit?.health_percent);
  $('signalDuration').textContent=`${signal.duration} tick${Number(signal.duration)===1?'':'s'}`;
  $('signalPayout').textContent=money(signal.payout);
  $('signalEv').textContent=money(signal.expected_value);
}
function renderDigits(decision){
  const rows=decision?.digits||[];
  $('digitRows').innerHTML=Array.from({length:10},(_,digit)=>{
    const row=rows.find(item=>Number(item.digit)===digit);
    if(!row)return `<tr><td class="digit-cell">${digit}</td><td class="num">—</td><td class="num">—</td><td class="decision-no">NO TRADE</td><td class="reason">Research data unavailable.</td></tr>`;
    const approved=row.decision==='TRADE';
    const reason=(row.reasons||[]).join(' ');
    return `<tr class="${approved?'row-trade':''}"><td class="digit-cell">${digit}</td><td class="num">${pct(row.estimated_win_probability)}</td><td class="num">${score(row.health_percent)}</td><td class="${approved?'decision-trade':'decision-no'}">${approved?'TRADE · PAPER':'NO TRADE'}</td><td class="reason">${safe(approved?'All required evidence and signal gates passed.':reason||'No qualifying per-digit evidence.')}</td></tr>`;
  }).join('');
}
function renderVolatility(decision){
  const data=decision?.volatility||{};
  $('volatilityCondition').textContent=data.condition||'INSUFFICIENT DATA';
  $('volatilityHealth').textContent=score(data.health_percent);
  const passed=data.status==='APPROVED';
  const approval=$('volatilityApproval');
  approval.textContent=passed?'PASS':data.status==='INSUFFICIENT_EVIDENCE'?'INSUFFICIENT DATA':'FAIL';
  approval.className=`value ${passed?'good-text':data.status==='INSUFFICIENT_EVIDENCE'?'warn-text':'bad-text'}`;
}
function renderPaper(performance){
  const total=Number(performance?.paper_opened||0);
  const wins=Number(performance?.wins||0),losses=Number(performance?.losses||0);
  const settled=Number(performance?.paper_settled||0),profit=performance?.net_paper_profit;
  $('tradeTotal').textContent=total.toLocaleString('en-US');
  $('tradeWins').textContent=String(wins);$('tradeLosses').textContent=String(losses);
  $('tradeWinRate').textContent=settled?`${(Number(performance.win_rate)*100).toFixed(1)}%`:'—';
  $('tradeProfit').textContent=profit==null?'—':money(profit);
  $('tradeProfit').className=`value ${profit==null?'':Number(profit)>=0?'good-text':'bad-text'}`;
}
function renderUnavailable(error){
  const connection=$('connection');connection.className='status bad';connection.querySelector('span').textContent='API ERROR';
  const pipelineStatus=$('pipelineStatus');pipelineStatus.className='status bad';pipelineStatus.querySelector('span').textContent='ERROR';
  for(const id of ['subscriptionStatus','analysisStatus','strategyStatus','generationStatus','tickCounts','signalsRecorded','missingTicks','badTicks'])$(id).textContent='—';
  $('pipelineError').textContent=`API unavailable: ${error.message}`;$('pipelineError').hidden=false;
  $('signalHistory').innerHTML=`<tr><td colspan="5" class="error">Signal history unavailable: ${safe(error.message)}</td></tr>`;
  $('latestTick').textContent='—';$('freshness').textContent='UNAVAILABLE';$('freshness').className='value bad-text';
  $('signalStatus').className='trade-pill no-trade';$('signalStatus').textContent='NO TRADE';
  $('signalDigit').className='signal-digit empty';$('signalDigit').textContent='NO QUALIFIED SIGNAL';
  for(const id of ['signalProbability','signalHealth','signalDuration','signalPayout','signalEv','volatilityCondition','volatilityHealth','volatilityApproval','tradeTotal','tradeWins','tradeLosses','tradeWinRate','tradeProfit'])$(id).textContent='—';
  $('digitRows').innerHTML=Array.from({length:10},(_,digit)=>`<tr><td class="digit-cell">${digit}</td><td class="num">—</td><td class="num">—</td><td class="decision-no">NO TRADE</td><td class="reason">API unavailable: ${safe(error.message)}</td></tr>`).join('');
}
async function refresh(){
  try{
    const [decision,history]=await Promise.all([
      api(`/decision?symbol=${encodeURIComponent(state.symbol)}`),
      api(`/signals/history?symbol=${encodeURIComponent(state.symbol)}&limit=10`)
    ]);
    renderMarket(decision);
    renderSignalHistory(history);
    renderSignal(decision);
    renderDigits(decision);
    renderVolatility(decision);
    renderPaper(decision.performance);
    $('updatedAt').textContent=`Updated ${new Date(decision.created_at).toLocaleTimeString()}`;
  }catch(error){
    renderUnavailable(error);$('updatedAt').textContent=`Update failed · ${new Date().toLocaleTimeString()}`;
  }
}
$('symbol').addEventListener('change',event=>{state.symbol=event.target.value;refresh()});
refresh();setInterval(refresh,3000);
</script>
</body>
</html>
```

## `bromax/desktop.py`

```python
"""Windows-friendly floating research panel for the existing BROMAX API."""
import argparse
import base64
import json
import os
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import threading
from tkinter import simpledialog, ttk


class AnalysisPanel:
    def __init__(self, root, base_url, symbol):
        self.root=root
        self.base_url=base_url.rstrip("/")
        self.symbol=symbol
        self.username=os.getenv("BROMAX_AUTH_USERNAME","")
        self.password=os.getenv("BROMAX_AUTH_PASSWORD","")
        self.compact=False
        self.topmost=True
        self.credentials_prompted=False
        self.request_in_progress=False
        root.title("BROMAX · Live analysis")
        root.geometry("400x350")
        root.minsize(340,300)
        root.resizable(True,True)
        root.configure(background="#101719")
        root.attributes("-topmost",True)
        self.style=ttk.Style(root)
        self.style.theme_use("clam")
        self.style.configure("TFrame",background="#101719")
        self.style.configure("Card.TFrame",background="#182124")
        self.style.configure("TLabel",background="#101719",foreground="#dce8e4",font=("Segoe UI",10))
        self.style.configure("Muted.TLabel",background="#101719",foreground="#91a29c",font=("Segoe UI",9))
        self.style.configure("Hero.TLabel",background="#182124",foreground="#85d2a3",font=("Segoe UI",30,"bold"))
        self.style.configure("Metric.TLabel",background="#182124",foreground="#ecf1ee",font=("Segoe UI",14,"bold"))
        self.style.configure("Treeview",background="#182124",foreground="#dce8e4",fieldbackground="#182124",rowheight=27,borderwidth=0)
        self.style.configure("Treeview.Heading",background="#202b2e",foreground="#9fada8",font=("Segoe UI",9,"bold"))
        self.style.map("Treeview",background=[("selected","#274b3a")])
        self._build()
        self.root.after(200,self.refresh)

    def _build(self):
        header=ttk.Frame(self.root,padding=(12,10))
        header.pack(fill="x")
        ttk.Label(header,text="BROMAX",font=("Segoe UI",12,"bold")).pack(side="left")
        ttk.Button(header,text="Login",command=self._login).pack(side="right",padx=(5,0))
        self.top_button=ttk.Button(header,text="Always on top",command=self.toggle_topmost)
        self.top_button.pack(side="right",padx=(5,0))
        self.compact_button=ttk.Button(header,text="Compact",command=self.toggle_compact)
        self.compact_button.pack(side="right")

        self.summary=ttk.Frame(self.root,style="Card.TFrame",padding=14)
        self.summary.pack(fill="x",padx=12,pady=(0,10))
        self.fields={}
        for key,title in (
            ("digit","Recommended digit"),("probability","Win probability"),
            ("health","Health"),("duration","Contract duration"),
            ("volatility","Volatility approval"),("status","TRADE STATUS"),
        ):
            row=ttk.Frame(self.summary,style="Card.TFrame")
            row.pack(fill="x",pady=4)
            ttk.Label(row,text=title,style="Muted.TLabel",width=22).pack(side="left")
            value=ttk.Label(row,text="—",style="Metric.TLabel")
            value.pack(side="right")
            self.fields[key]=value
        actions=ttk.Frame(self.root,padding=(12,0,12,10))
        actions.pack(fill="x")
        ttk.Button(actions,text="Open dashboard",command=self.open_dashboard).pack(side="left")
        ttk.Button(actions,text="Hide",command=self.root.iconify).pack(side="right")

    def _request(self):
        headers={"Accept":"application/json"}
        if self.username and self.password:
            token=base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            headers["Authorization"]=f"Basic {token}"
        symbol=urllib.parse.quote(self.symbol)
        request=urllib.request.Request(f"{self.base_url}/decision?symbol={symbol}",headers=headers)
        with urllib.request.urlopen(request,timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def refresh(self):
        if self.request_in_progress:
            return
        self.request_in_progress=True
        threading.Thread(target=self._fetch_analysis,daemon=True).start()

    def _fetch_analysis(self):
        try:
            data=self._request()
            result=(data,None)
        except urllib.error.HTTPError as error:
            result=(None,("http",error.code))
        except (urllib.error.URLError,TimeoutError,ValueError,json.JSONDecodeError) as error:
            result=(None,("connection",str(error)))
        self.root.after(0,self._finish_refresh,*result)

    def _finish_refresh(self,data,error):
        self.request_in_progress=False
        if error is None:
            self._render(data)
        elif error[0]=="http":
            code=error[1]
            if code==401:
                if not (self.username and self.password) and not self.credentials_prompted:
                    self._ask_credentials()
            self._render_unavailable(f"API ERROR {code}")
        else:
            self._render_unavailable("API OFFLINE")
        self.root.after(2500,self.refresh)

    def _login(self):
        self.credentials_prompted=False
        self._ask_credentials()

    def _ask_credentials(self):
        self.credentials_prompted=True
        username=simpledialog.askstring("BROMAX login","API username:",parent=self.root)
        if not username:
            return
        password=simpledialog.askstring("BROMAX login","API password:",show="*",parent=self.root)
        if password:
            self.username=username
            self.password=password
            self.credentials_prompted=False
            self.root.after(50,self.refresh)

    def _render(self,data):
        signal=data.get("signal") if data.get("trade_authorized") is True else None
        digit_row=next((row for row in data.get("digits",[])
                        if signal and int(row.get("digit",-1))==int(signal.get("predicted_digit",-2))),None)
        qualified=bool(signal and digit_row and digit_row.get("decision")=="TRADE")
        volatility=data.get("volatility") or {}
        if not qualified:
            for key in ("probability","health","duration"):
                self.fields[key].configure(text="—")
            self.fields["digit"].configure(text="NO QUALIFIED SIGNAL")
            self.fields["volatility"].configure(text=volatility.get("status","INSUFFICIENT_EVIDENCE"))
            self.fields["status"].configure(text="NO TRADE",foreground="#d78e88")
            return
        self.fields["digit"].configure(text=str(signal["predicted_digit"]))
        self.fields["probability"].configure(text=f"{float(signal['probability'])*100:.1f}%")
        health=digit_row.get("health_percent")
        self.fields["health"].configure(text=f"{health}%" if health is not None else "—")
        self.fields["duration"].configure(text=f"{signal['duration']} tick(s)")
        self.fields["volatility"].configure(text="PASS" if volatility.get("status")=="APPROVED" else "FAIL")
        self.fields["status"].configure(text="TRADE · PAPER ONLY",foreground="#85d2a3")

    def _render_unavailable(self,status):
        for key in ("digit","probability","health","duration","volatility"):
            self.fields[key].configure(text="—")
        self.fields["digit"].configure(text="NO QUALIFIED SIGNAL")
        status_text="NO TRADE" if status=="NO TRADE" else f"NO TRADE · {status}"
        self.fields["status"].configure(text=status_text,foreground="#d78e88")

    def toggle_topmost(self):
        self.topmost=not self.topmost
        self.root.attributes("-topmost",self.topmost)
        self.top_button.configure(text="Always on top" if self.topmost else "Normal window")

    def toggle_compact(self):
        self.compact=not self.compact
        if self.compact:
            self.root.geometry("350x320")
            self.compact_button.configure(text="Expand")
        else:
            self.root.geometry("400x350")
            self.compact_button.configure(text="Compact")

    def open_dashboard(self):
        webbrowser.open(f"{self.base_url}/dashboard")


def main():
    parser=argparse.ArgumentParser(description="BROMAX floating research panel")
    parser.add_argument("--url",default=os.getenv("BROMAX_API_URL","http://127.0.0.1:8765"))
    parser.add_argument("--symbol",default=os.getenv("DERIV_SYMBOLS","1HZ100V").split(",")[0].strip())
    args=parser.parse_args()
    root=tk.Tk()
    AnalysisPanel(root,args.url,args.symbol)
    root.mainloop()


if __name__=="__main__":
    main()
```

## `bromax/digits.py`

```python
from decimal import Decimal, InvalidOperation

class QuotePrecisionError(ValueError):
    pass

def quote_precision(pip_size: int | str | Decimal) -> int:
    """Deriv tick.pip_size is the number of decimal places to display (toFixed precision)."""
    try:
        d = Decimal(str(pip_size))
    except (InvalidOperation, ValueError) as exc:
        raise QuotePrecisionError(f"Invalid pip_size: {pip_size!r}") from exc
    if not d.is_finite() or d != d.to_integral_value() or not 0 <= int(d) <= 10:
        raise QuotePrecisionError(f"pip_size must be an integer precision from 0 to 10, got {pip_size!r}")
    return int(d)

def extract_last_digit(quote: str | int | Decimal, pip_size: int | str | Decimal) -> tuple[str, int | None]:
    """Format quote to Deriv's symbol precision, then take the final displayed decimal digit.

    No float conversion is used. Trailing zeroes are restored from pip_size, not
    inferred from the JSON number's lexical representation.
    """
    precision = quote_precision(pip_size)
    if isinstance(quote, float):
        raise QuotePrecisionError("Binary float quotes are not accepted; parse the raw JSON number as Decimal")
    try:
        value = quote if isinstance(quote, Decimal) else Decimal(str(quote))
    except (InvalidOperation, ValueError) as exc:
        raise QuotePrecisionError(f"Malformed quote: {quote!r}") from exc
    if not value.is_finite():
        raise QuotePrecisionError(f"Non-finite quote: {quote!r}")
    quantum = Decimal(1).scaleb(-precision)
    try:
        displayed = value.quantize(quantum)
    except InvalidOperation as exc:
        raise QuotePrecisionError(f"Quote cannot be represented at precision {precision}: {quote!r}") from exc
    if displayed != value:
        raise QuotePrecisionError(f"Quote {quote!r} exceeds declared precision {precision}")
    text = f"{displayed:.{precision}f}"
    if precision == 0:
        return text, None
    return text, int(text[-1])
```

## `bromax/intelligence.py`

```python
"""Exploratory, causal digit-pattern and market-state diagnostics.

These diagnostics describe history; they are not proof of predictive edge and do
not independently authorize paper trades. Candidate strategies are evaluated by
research.py on chronological unseen data before any approval is possible.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import math
import numpy as np
from scipy.stats import chi2


def _wilson(k: int, n: int, z: float = 1.959963984540054):
    if n <= 0:
        return None
    p = k / n
    den = 1 + z*z/n
    center = (p + z*z/(2*n))/den
    half = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/den
    return [max(0.0, center-half), min(1.0, center+half)]


def _sequence_stats(digits: list[int], order: int):
    if len(digits) <= order:
        return {"order": order, "context": digits[-order:] if digits else [], "support": 0,
                "successor_counts": {str(i): 0 for i in range(10)}, "probabilities": None,
                "top_digit": None, "top_probability": None, "historical_hit_rate_ci95": None}
    context = tuple(digits[-order:])
    counts = Counter()
    hits = 0
    n = 0
    # Every target is historical and has a complete preceding context.
    for j in range(order, len(digits)):
        if tuple(digits[j-order:j]) == context:
            counts[digits[j]] += 1
            n += 1
    # Current context is the final order digits; it has no known successor yet.
    total = sum(counts.values())
    smooth = np.asarray([counts[i] + 1.0 for i in range(10)], dtype=float)
    probs = smooth / (total + 10.0)
    top = int(np.argmax(probs))
    hits = counts[top]
    return {"order": order, "context": list(context), "support": total,
            "successor_counts": {str(i): int(counts[i]) for i in range(10)},
            "probabilities": [float(x) for x in probs], "top_digit": top,
            "top_probability": float(probs[top]),
            "historical_hit_rate_ci95": _wilson(hits, total) if total else None,
            "warning": "Exploratory historical conditional frequency; selection and reuse create bias. Not a validated forecast."}


def analyze_ticks(ticks, symbol: str, window: int = 1000):
    rows = ticks[-max(100, min(int(window), 10000)):]
    digits = [int(r["digit"] if isinstance(r, dict) else r.digit) for r in rows]
    n = len(digits)
    counts = np.bincount(digits, minlength=10) if n else np.zeros(10, dtype=int)
    expected = n / 10 if n else 0
    statistic = float(np.sum((counts-expected)**2/expected)) if expected else None
    pvalue = float(chi2.sf(statistic, 9)) if statistic is not None else None
    transition = np.zeros((10, 10), dtype=int)
    for a, b in zip(digits[:-1], digits[1:]):
        transition[a, b] += 1
    row_sums = transition.sum(axis=1)
    transition_probs = [[float((transition[i,j]+1)/(row_sums[i]+10)) for j in range(10)] for i in range(10)]
    run = 0
    if digits:
        last = digits[-1]
        for d in reversed(digits):
            if d != last: break
            run += 1
    price_changes = []
    for r in rows:
        value = r.get("quote_text") if isinstance(r, dict) else getattr(r, "quote_text", None)
        if value is None: continue
        try:
            price = Decimal(str(value))
            if price.is_finite(): price_changes.append(float(price))
        except (InvalidOperation, ValueError):
            continue
    abs_returns = []
    for a, b in zip(price_changes[:-1], price_changes[1:]):
        if a != 0 and math.isfinite(a) and math.isfinite(b):
            abs_returns.append(abs((b-a)/a))
    short_vol = float(np.mean(abs_returns[-100:])) if abs_returns else None
    long_vol = float(np.mean(abs_returns)) if abs_returns else None
    return {
        "symbol": symbol, "window": len(digits), "sample_size": n,
        "digit_counts": {str(i): int(counts[i]) for i in range(10)},
        "digit_frequencies": {str(i): (float(counts[i]/n) if n else None) for i in range(10)},
        "uniformity_diagnostic": {"chi_square_statistic": statistic, "degrees_of_freedom": 9 if n else None,
                                  "p_value": pvalue,
                                  "interpretation": "Descriptive goodness-of-fit diagnostic only; serial dependence and repeated testing are not accounted for."},
        "patterns": {"last_digit_run_length": run, "conditional_order_1": _sequence_stats(digits, 1),
                     "conditional_order_2": _sequence_stats(digits, 2),
                     "conditional_order_3": _sequence_stats(digits, 3)},
        "transition_matrix_smoothed": transition_probs,
        "market_state": {"absolute_return_observations": len(abs_returns),
                         "mean_absolute_return_last_100": short_vol,
                         "mean_absolute_return_full_window": long_vol,
                         "short_to_long_volatility_ratio": (short_vol/long_vol if short_vol is not None and long_vol not in (None, 0) else None),
                         "note": "Price-return diagnostics are descriptive, not evidence that digit outcomes are forecastable."},
        "signal_status": "RESEARCH_ONLY_NOT_APPROVED",
        "disclaimer": "Pattern recurrence and frequency imbalance can occur by chance. Only pre-registered out-of-sample testing with actual contract quotes can establish evidence of edge."
    }
```

## `bromax/paperbot.py`

```python
from datetime import datetime, timezone
import time
from decimal import Decimal
import json
from pathlib import Path
from threading import Event

_LIVE_KILL_SWITCH = Event()
_LIVE_KILL_SWITCH.set()

class ExecutionInterface:
    """Fail-closed execution boundary with a one-way emergency kill switch."""
    enabled = False

    def __init__(self):
        self._kill_switch = _LIVE_KILL_SWITCH

    @property
    def kill_switch_engaged(self):
        return self._kill_switch.is_set()

    def engage_kill_switch(self):
        self._kill_switch.set()

    async def execute(self, *args, **kwargs):
        if self.kill_switch_engaged:
            raise RuntimeError("Emergency kill switch is engaged; execution is blocked")
        raise RuntimeError("Live execution is intentionally not implemented or enabled")

class PaperBot:
    def __init__(self, store, stake: Decimal, max_stake: Decimal, daily_loss_cap: Decimal, max_trades: int, approvals_path="approved_strategies.json"):
        self.store=store; self.stake=stake; self.max_stake=max_stake; self.daily_loss_cap=daily_loss_cap; self.max_trades=max_trades
        self.approvals_path=Path(approvals_path)
    def approved(self, symbol, strategy_id):
        # Two-key gate: explicit user approval plus machine-checked validation and untouched-test evidence.
        if not self.approvals_path.exists(): return False
        try: approvals=json.loads(self.approvals_path.read_text())
        except Exception: return False
        if strategy_id not in approvals.get("approved",{}).get(symbol,[]): return False
        report_path=Path("reports/bromax_report.json")
        if not report_path.exists(): return False
        try: report=json.loads(report_path.read_text())
        except Exception: return False
        for item in report.get("symbols",[]):
            if item.get("symbol") != symbol or strategy_id not in item.get("validation_finalists",[]): continue
            snapshot=item.get("research_snapshot",{})
            if not snapshot.get("frozen") or snapshot.get("protocol_version") != "1.1.0": return False
            result=next((x for x in item.get("strategies",[]) if x.get("strategy_id")==strategy_id),None)
            if not result or not result.get("final_test"): return False
            test=result["final_test"]; ci=test.get("payout_adjusted",{}).get("realized_profit_ci95")
            ev=test.get("payout_adjusted",{})
            hit_ci=test.get("hit_rate_block_bootstrap_ci95")
            return bool(result.get("final_test_verdict")=="evidence_of_edge" and test.get("n",0)>=5000 and ev.get("n_with_actual_quote",0)>=5000 and ev.get("quote_coverage",0)>=.90 and ci and ci[0]>0 and hit_ci and hit_ci[0]>.10 and test.get("holm_adjusted_p",1)<.05)
        return False
    def limits_ok(self, symbol, next_stake=None):
        today=datetime.now(timezone.utc).date().isoformat()
        next_stake=Decimal(next_stake if next_stake is not None else self.stake)
        if next_stake > self.max_stake: return False
        with self.store.connect() as c:
            settled_rows=c.execute("SELECT paper_pnl FROM audit_events WHERE event_type='PAPER_SETTLE' AND substr(created_utc,1,10)=?",(today,)).fetchall()
            open_rows=c.execute("SELECT o.stake FROM audit_events o WHERE o.event_type='PAPER_OPEN' AND NOT EXISTS (SELECT 1 FROM audit_events z WHERE z.event_type='PAPER_SETTLE' AND z.note=CAST(o.seq AS TEXT))").fetchall()
        open_count=len(open_rows)
        open_risk=sum((Decimal(x[0]) for x in open_rows),Decimal("0"))
        realized=sum((Decimal(x[0]) for x in settled_rows if x[0] is not None),Decimal("0"))
        # Reserve the full stake of every open paper contract as worst-case loss.
        return (len(settled_rows)+open_count < self.max_trades and realized-open_risk-next_stake > -self.daily_loss_cap)
    def record(self, symbol, forecast, forecast_seq, tick_id, epoch, stake, payout, proposal_id, spot_time):
        if not self.approved(symbol,forecast.strategy_id): return "strategy_not_approved"
        if payout is None: return "missing_live_proposal"
        if spot_time is None or int(spot_time) != int(epoch): return "proposal_spot_time_mismatch"
        if stake > self.max_stake: return "quoted_stake_exceeds_max"
        probability=Decimal(str(forecast.probabilities[forecast.prediction]))
        if probability*Decimal(payout)-Decimal(stake) <= 0: return "current_quote_model_ev_nonpositive"
        # Require quote-backed, untouched out-of-sample profitability for the
        # exact strategy and the one-tick duration supported by this executor.
        from .timing import evaluate_payout_timing
        payout_report=evaluate_payout_timing(self.store.duration_events(symbol))
        best=payout_report.get("best_candidate")
        if not best or best.get("strategy_id") != forecast.strategy_id or best.get("duration_ticks") != 1:
            return "duration_specific_payout_edge_not_confirmed_for_one_tick"
        # Apply the same fail-closed volatility confirmation at the actual
        # paper-entry boundary, not only in the read-only READY endpoint.
        from .timing import evaluate_timing
        timing_report=evaluate_timing(self.store.history(symbol,limit=10000),self.store.forecasts(symbol,limit=50000))
        regime=timing_report.get("current_volatility_regime")
        timing_row=next((x for x in timing_report.get("strategies",[]) if x.get("strategy_id")==forecast.strategy_id and x.get("duration_ticks")==1),None)
        regime_row=next((x for x in (timing_row or {}).get("volatility_regimes",[]) if x.get("regime")==regime),None)
        if not regime_row or regime_row.get("status")!="CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON":
            return "volatility_confirmation_not_established"
        if not self.limits_ok(symbol,stake): return "paper_limits_blocked"
        if time.time() >= epoch + 1: return "quote_arrived_after_next_tick_deadline"
        if not self.store.is_latest_tick(symbol,tick_id): return "quote_arrived_after_next_tick"
        # Paper-only open; Store appends settlement on the immediately following tick.
        self.store.append_paper_open(symbol,forecast,forecast_seq,tick_id,epoch,stake,payout,proposal_id,spot_time)
        return "paper_trade_opened"
```

## `bromax/proposals.py`

```python
import asyncio, json, itertools
from decimal import Decimal
import websockets

class ProposalUnavailable(RuntimeError): pass

def encode_proposal_request(symbol, digit, stake, currency, duration_ticks, req_id):
    if isinstance(stake,float): raise ValueError("Binary float stake is not accepted; pass Decimal")
    stake=Decimal(stake)
    if not stake.is_finite() or stake <= 0: raise ValueError("Proposal stake must be a positive finite Decimal")
    request={"proposal":1,"amount":"__BROMAX_DECIMAL_AMOUNT__","basis":"stake","contract_type":"DIGITMATCH",
             "currency":currency,"duration":duration_ticks,"duration_unit":"t",
             "barrier":str(digit),"underlying_symbol":symbol,"req_id":req_id}
    # Emit Decimal as an exact JSON number; do not round money through binary float.
    return json.dumps(request).replace('"__BROMAX_DECIMAL_AMOUNT__"',format(stake,"f"))

class ProposalClient:
    """Unauthenticated quote-only proposal client. It never calls buy/sell."""
    def __init__(self, ws_url, currency="USD", timeout=4.0):
        self.ws_url, self.currency, self.timeout = ws_url, currency, timeout
        self._ids = itertools.count(1000)
    async def quote(self, symbol: str, digit: int, stake: Decimal, duration_ticks: int = 1):
        req_id=next(self._ids)
        request=encode_proposal_request(symbol,digit,stake,self.currency,duration_ticks,req_id)
        try:
            async with websockets.connect(self.ws_url,ping_interval=20,ping_timeout=20,close_timeout=3) as ws:
                await ws.send(request)
                async with asyncio.timeout(self.timeout):
                    while True:
                        msg=json.loads(await ws.recv(),parse_float=Decimal)
                        if msg.get("req_id") != req_id: continue
                        if msg.get("error"): raise ProposalUnavailable(msg["error"].get("message","proposal error"))
                        p=msg.get("proposal")
                        if p:
                            payout=Decimal(str(p["payout"])); ask=Decimal(str(p["ask_price"])); spot_time=int(p["spot_time"])
                            if payout <= 0 or ask <= 0 or spot_time <= 0: raise ProposalUnavailable("Invalid proposal payout, ask price, or spot_time")
                            return {"payout":payout,"stake":ask,"proposal_id":str(p.get("id","")),"spot":str(p.get("spot","")),"spot_time":spot_time}
        except Exception as exc:
            if isinstance(exc, ProposalUnavailable): raise
            raise ProposalUnavailable(str(exc)) from exc

    async def quote_many(self, requests):
        """Fetch many read-only proposals over one WebSocket connection.

        requests: iterable of (symbol, digit, Decimal stake, duration_ticks).
        A single connection reduces latency for 1-second tick quote capture.
        """
        requests=list(requests)
        if not requests: return {}
        pending={}; output={}
        try:
            async with websockets.connect(self.ws_url,ping_interval=20,ping_timeout=20,close_timeout=3) as ws:
                for symbol,digit,stake,duration in requests:
                    req_id=next(self._ids)
                    payload=encode_proposal_request(symbol,digit,stake,self.currency,duration,req_id)
                    pending[req_id]=(symbol,int(digit),int(duration))
                    await ws.send(payload)
                async with asyncio.timeout(self.timeout):
                    while pending:
                        msg=json.loads(await ws.recv(),parse_float=Decimal)
                        req_id=msg.get('req_id')
                        if req_id not in pending: continue
                        key=pending.pop(req_id)
                        if msg.get('error'):
                            output[key]=None; continue
                        p=msg.get('proposal')
                        if not p:
                            output[key]=None; continue
                        payout=Decimal(str(p['payout'])); ask=Decimal(str(p['ask_price'])); spot_time=int(p['spot_time'])
                        if payout<=0 or ask<=0 or spot_time<=0:
                            output[key]=None; continue
                        output[key]={'payout':payout,'stake':ask,'proposal_id':str(p.get('id','')),'spot':str(p.get('spot','')),'spot_time':spot_time}
            return output
        except Exception as exc:
            raise ProposalUnavailable(str(exc)) from exc
```

## `bromax/reporting.py`

```python
import json
from pathlib import Path
from .research import generate_report, _block_bootstrap_ci

def _paper_summary(events):
    settled=[e for e in events if e["event_type"]=="PAPER_SETTLE"]
    opened=[e for e in events if e["event_type"]=="PAPER_OPEN"]
    settled_open_ids={e["note"] for e in settled}
    open_count=sum(1 for e in opened if str(e["seq"]) not in settled_open_ids)
    pnls=[float(e["paper_pnl"]) for e in settled if e.get("paper_pnl") is not None]
    wins=sum(int(e["hit"] or 0) for e in settled)
    return {
        "status":"descriptive_only_not_proof",
        "opened_contracts":len(opened),"settled_contracts":len(settled),"still_open":open_count,
        "wins":wins,"losses":len(settled)-wins,
        "hit_rate":wins/len(settled) if settled else None,
        "realized_profit_total":sum(pnls) if pnls else 0.0,
        "mean_realized_profit_per_contract":sum(pnls)/len(pnls) if pnls else None,
        "realized_profit_ci95":_block_bootstrap_ci(pnls,seed=421) if pnls else None,
    }

def write_report(store, symbols, path="reports/bromax_report.json"):
    report=generate_report(store,symbols)
    for item in report["symbols"]:
        events=store.paper_events(item["symbol"])
        item["paper_forward_test"]=_paper_summary(events)
        strategy_ids=sorted({e["strategy_id"] for e in events})
        by_strategy=[]
        for strategy_id in strategy_ids:
            by_strategy.append({"strategy_id":strategy_id,**_paper_summary([e for e in events if e["strategy_id"]==strategy_id])})
        item["paper_forward_test_by_strategy"]=by_strategy
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(report,indent=2,default=str))
    return report
```

## `bromax/research.py`

```python
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from scipy.stats import binomtest
from .strategies import candidate_strategies, ForecastContext, Forecast, CumulativeMode

PROTOCOL_VERSION = "1.1.0"

PRE_REGISTERED = {
    "protocol_version": PROTOCOL_VERSION,
    "candidate_ids": [s.strategy_id for s in candidate_strategies()],
    "strategy_versions": {s.strategy_id:s.version for s in candidate_strategies()},
    "minimum_history_ticks": 1000,
    "contract_type": "DIGITMATCH",
    "contract_duration_ticks": 1,
    "split": {"development": 0.60, "validation": 0.20, "untouched_test": 0.20},
    "windows": [100, 500, 1000],
    "laplace_alpha": [1.0],
    "exponential_half_lives_ticks": [50, 200],
    "conditional_pattern_candidate": {"strategy_id": "conditional_transition_order2", "order": 2, "alpha": 1.0, "minimum_support_for_interpretation": 20, "low_support_fallback": "laplace_frequency_500"},
    "tie_break": "lowest digit",
    "selection": "rank development by multiclass Brier; gate on validation only",
    "validation_gates": {"minimum_n": 5000, "minimum_quoted_n": 5000, "minimum_quote_coverage": 0.90, "Holm_adjusted_one_sided_p_lt": 0.05, "hit_rate_block_CI95_lower_gt": 0.10, "realized_profit_CI95_lower_gt": 0.0},
    "multiple_comparison_scope": "Holm correction per symbol across the pre-registered strategy family",
    "final_test": "evaluate only validation finalists; no tuning or reselection on final test",
}

def wilson(k, n, z=1.959963984540054):
    if n == 0: return (None, None)
    p=k/n; den=1+z*z/n; centre=(p+z*z/(2*n))/den
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return centre-half,centre+half

def holm_adjust(pvalues):
    p=np.asarray(pvalues,dtype=float); m=len(p); order=np.argsort(p); out=np.empty(m); running=0.0
    for rank,idx in enumerate(order):
        running=max(running,(m-rank)*p[idx]); out[idx]=min(1.0,running)
    return out.tolist()

def reliability_table(probabilities, actuals):
    if not probabilities: return []
    bins=[0,.10,.15,.20,.30,.40,.50,.60,.70,.80,.90,1.000001]; rows=[]
    for lo,hi in zip(bins[:-1],bins[1:]):
        sub=[(p,a) for p,a in zip(probabilities,actuals) if lo <= max(p) < hi]
        if sub:
            rows.append({"top_probability_bin":f"[{lo:.2f},{hi:.2f})","n":len(sub),"mean_predicted_top_probability":float(np.mean([max(p) for p,a in sub])),"observed_top_digit_hit_rate":float(np.mean([int(np.argmax(p)==a) for p,a in sub]))})
    return rows

def _forecast_frame(ticks: pd.DataFrame, symbol: str):
    """Fast, causal batch replay. Each state is updated with tick i before forecasting i+1."""
    from collections import deque
    from .strategies import UniformBaseline
    records=[]
    digits=ticks.digit.astype(int).tolist(); ids=ticks.id.astype(int).tolist(); epochs=ticks.epoch.astype(int).tolist()
    windows=(100,500,1000)
    queues={w:deque() for w in windows}; counts={w:np.zeros(10,dtype=np.int64) for w in windows}
    half_lives=(50.0,200.0); decays={h:math.exp(-math.log(2)/h) for h in half_lives}; exp_counts={h:np.zeros(10,dtype=float) for h in half_lives}; exp_queues={h:deque() for h in half_lives}
    baseline=UniformBaseline(); cumulative_counts=np.zeros(10,dtype=np.int64); transition_counts={}; transition_events=deque()
    for i,digit in enumerate(digits[:-1]):
        cumulative_counts[digit]+=1
        for w in windows:
            q=queues[w]; q.append(digit); counts[w][digit]+=1
            if len(q)>w: counts[w][q.popleft()]-=1
        for h in half_lives:
            exp_counts[h] *= decays[h]; exp_counts[h][digit] += 1.0
            exp_queues[h].append(digit)
            if len(exp_queues[h])>1000:
                expired=exp_queues[h].popleft(); exp_counts[h][expired] -= decays[h]**1000
        if i >= 2:
            prior_pair = (digits[i-2], digits[i-1])
            if len(transition_events) >= 998:
                expired_pair, expired_digit = transition_events.popleft()
                transition_counts[expired_pair][expired_digit] -= 1
            transition_events.append((prior_pair, digit))
            transition_counts.setdefault(prior_pair, np.zeros(10, dtype=np.int64))[digit] += 1
        ctx=ForecastContext(symbol,ids[i],epochs[i],tuple(digits[:i+1]),tuple(int(x) for x in cumulative_counts))
        if i+1 < 1000: continue
        forecasts=[]
        f=baseline.forecast(ctx)
        forecasts.append(f)
        forecasts.append(CumulativeMode().forecast(ctx))
        for w in windows:
            c=counts[w].astype(float); probs=c/c.sum()
            forecasts.append(Forecast(f"rolling_mode_{w}","1.0.0",{"window":w,"tie_break":"lowest_digit"},int(np.argmax(probs)),tuple(probs)))
        for w in (100,500):
            c=counts[w].astype(float); probs=(c+1.0)/(c.sum()+10.0)
            forecasts.append(Forecast(f"laplace_freq_{w}_a1", "1.0.0",{"window":w,"alpha":1.0,"prior":"symmetric_dirichlet"},int(np.argmax(probs)),tuple(probs)))
        for h in half_lives:
            c=exp_counts[h]; probs=(c+1.0)/(c.sum()+10.0)
            forecasts.append(Forecast(f"exp_frequency_hl{h:g}","1.0.0",{"half_life_ticks":h,"weight":"exp(-ln(2)*age/half_life)","alpha":1.0},int(np.argmax(probs)),tuple(probs)))
        pair = (digits[i-1], digits[i]) if i >= 1 else None
        tc = transition_counts.get(pair, np.zeros(10, dtype=np.int64)) if pair is not None else np.zeros(10, dtype=np.int64)
        conditional_source = tc.astype(float) if tc.sum() >= 20 else counts[500].astype(float)
        cond_probs = (conditional_source+1.0)/(conditional_source.sum()+10.0)
        forecasts.append(Forecast("conditional_transition_order2", "1.0.0", {"order":2,"alpha":1.0,"minimum_support_for_interpretation":20,"low_support_fallback":"laplace_frequency_500"}, int(np.argmax(cond_probs)), tuple(float(x) for x in cond_probs)))
        # Only after every forecast is fixed do we inspect the next tick for scoring.
        if epochs[i+1] != epochs[i] + 1: continue
        actual_digit=digits[i+1]
        for f in forecasts:
            records.append({"symbol":symbol,"tick_index":i,"forecast_tick_id":ids[i],"epoch":epochs[i],"strategy_id":f.strategy_id,"version":f.version,"prediction":f.prediction,"actual":actual_digit,"hit":int(f.prediction==actual_digit),"probabilities":f.probabilities})
    frame=pd.DataFrame(records)
    if not frame.empty:
        n=len(ticks); dev=int(n*.60); val=int(n*.80)
        frame["split"]=np.where(frame.tick_index<dev,"development",np.where(frame.tick_index<val,"validation","final_test"))
        frame["volatility_regime"]=_volatility_regimes(ticks,frame.tick_index.tolist())
    return frame

def _volatility_regimes(ticks: pd.DataFrame, tick_indices):
    if "quote_text" not in ticks or ticks.empty:
        return [None for _ in tick_indices]
    prices=pd.to_numeric(ticks["quote_text"],errors="coerce").to_numpy(dtype=float)
    returns=np.full(len(prices),np.nan)
    if len(prices)>1:
        previous=prices[:-1]; current=prices[1:]
        valid=np.isfinite(previous)&np.isfinite(current)&(previous!=0)
        returns[1:][valid]=np.abs((current[valid]-previous[valid])/previous[valid])
    volatility=np.full(len(prices),np.nan)
    for i in range(len(prices)):
        sample=returns[max(1,i-19):i+1]
        sample=sample[np.isfinite(sample)]
        if len(sample)>=10:
            volatility[i]=float(np.mean(sample))
    training=volatility[:max(1,int(len(volatility)*.60))]
    training=training[np.isfinite(training)]
    if len(training)<30:
        return [None for _ in tick_indices]
    low,high=np.quantile(training,[1/3,2/3])
    output=[]
    for index in tick_indices:
        value=volatility[int(index)]
        output.append(None if not np.isfinite(value) else "LOW" if value<=low else "MID" if value<=high else "HIGH")
    return output

def _block_bootstrap_ci(values, seed=417, block_length=50, repetitions=2000):
    arr=np.asarray(values,dtype=float); n=len(arr)
    if n==0: return None
    if n==1: return [float(arr[0]),float(arr[0])]
    rng=np.random.default_rng(seed); length=min(block_length,n); nblocks=math.ceil(n/length)
    means=np.empty(repetitions,dtype=float)
    for j in range(repetitions):
        starts=rng.integers(0,n-length+1,size=nblocks)
        sample=np.concatenate([arr[a:a+length] for a in starts])[:n]
        means[j]=sample.mean()
    return [float(np.quantile(means,.025)),float(np.quantile(means,.975))]

def _ev_stats(rows):
    usable=[r for r in rows if r.get("payout") not in (None,"") and r.get("stake") not in (None,"") and r.get("probabilities") and r.get("actual") is not None]
    if not usable: return {"n_with_actual_quote":0,"mean_realized_profit_per_contract":None,"realized_profit_ci95":None,"mean_model_expected_value":None,"model_expected_value_ci95":None,"mean_break_even_hit_rate":None,"observed_hit_rate_minus_mean_break_even":None,"hit_rate_minus_break_even_ci95":None,"quote_coverage":0.0,"bootstrap_block_length":50}
    realized=[]; model_ev=[]; be=[]; hit_minus_be=[]
    for r in usable:
        prob=float(r["probabilities"][int(r["prediction"])])
        payout=float(r["payout"]); stake=float(r["stake"]); hit=int(r["hit"])
        realized.append((payout-stake) if hit else -stake)
        model_ev.append(prob*payout-stake)
        be_rate=stake/payout; be.append(be_rate); hit_minus_be.append(hit-be_rate)
    return {"n_with_actual_quote":len(realized),"mean_realized_profit_per_contract":float(np.mean(realized)),
            "realized_profit_ci95":_block_bootstrap_ci(realized),"mean_model_expected_value":float(np.mean(model_ev)),
            "model_expected_value_ci95":_block_bootstrap_ci(model_ev,seed=418),"mean_break_even_hit_rate":float(np.mean(be)),
            "observed_hit_rate_minus_mean_break_even":float(np.mean(hit_minus_be)),
            "hit_rate_minus_break_even_ci95":_block_bootstrap_ci(hit_minus_be,seed=422),
            "quote_coverage":len(usable)/max(1,len(rows)),"bootstrap_block_length":min(50,len(realized))}

def _summary(group, quote_map):
    n=len(group); k=int(group.hit.sum()) if n else 0; ci=wilson(k,n)
    p=binomtest(k,n,.1,alternative="greater").pvalue if n else 1.0
    probs=list(group.probabilities) if n else []; actuals=list(group.actual.astype(int)) if n else []
    brier=float(np.mean([np.sum((np.asarray(pr)-np.eye(10)[a])**2) for pr,a in zip(probs,actuals)])) if n else None
    predicted_probs=[float(np.asarray(r["probabilities"],dtype=float)[int(r["prediction"])]) for r in group.to_dict("records")] if n else []
    mean_predicted_probability=float(np.mean(predicted_probs)) if predicted_probs else None
    rows=[]
    for r in group.to_dict("records"):
        q=quote_map.get((r["strategy_id"],r["version"],int(r["forecast_tick_id"])))
        rows.append({**r,**(q or {})})
    ev=_ev_stats(rows)
    hit_block_ci=_block_bootstrap_ci(group.hit.astype(int).tolist(),seed=419) if n else None
    return {"n":n,"hits":k,"hit_rate":k/n if n else None,"hit_rate_ci95":list(ci) if n else None,"hit_rate_block_bootstrap_ci95":hit_block_ci,
            "p_vs_uniform_10pct_one_sided":float(p),"brier_multiclass_10class":brier,
            "mean_predicted_probability":mean_predicted_probability,
            "absolute_calibration_error":abs(mean_predicted_probability-k/n) if n and mean_predicted_probability is not None else None,
            "reliability":reliability_table(probs,actuals),"payout_adjusted":ev}

def _digit_evidence(frame, quote_map, selected_strategy_id, validation_finalists, test_frame, test_raw, n_ticks):
    if not selected_strategy_id:
        return {"selected_strategy_id":None,"health_definition":"Evidence quality index; never a win probability.","digits":[]}
    strategy=frame[frame.strategy_id==selected_strategy_id]
    validation=strategy[strategy.split=="validation"]
    final_test=test_frame[test_frame.strategy_id==selected_strategy_id]
    final_opened=selected_strategy_id in validation_finalists
    fold_edges=np.linspace(0,int(n_ticks*.80),5,dtype=int)
    output=[]
    validation_summaries=[]
    final_summaries=[]
    for digit in range(10):
        val_group=validation[validation.prediction==digit]
        test_group=final_test[final_test.prediction==digit] if final_opened else final_test.iloc[0:0]
        val_summary=_summary(val_group,quote_map)
        test_summary=_summary(test_group,quote_map) if final_opened else None
        validation_summaries.append(val_summary)
        final_summaries.append(test_summary)
    val_adjusted=holm_adjust([x["p_vs_uniform_10pct_one_sided"] for x in validation_summaries])
    test_adjusted=holm_adjust([x["p_vs_uniform_10pct_one_sided"] for x in final_summaries if x is not None])
    test_adjusted_by_digit=dict(zip([i for i,x in enumerate(final_summaries) if x is not None],test_adjusted))

    for digit in range(10):
        val_summary=validation_summaries[digit]
        test_summary=final_summaries[digit]
        val_summary["holm_adjusted_p_across_digits"]=val_adjusted[digit]
        if test_summary is not None:
            test_summary["holm_adjusted_p_across_digits"]=test_adjusted_by_digit[digit]
        history_group=strategy[strategy.prediction==digit]
        recent_start=max(0,int(history_group.tick_index.max())-499) if not history_group.empty else 0
        recent_group=history_group[history_group.tick_index>=recent_start]
        recent_n=len(recent_group)
        recent_hit_rate=float(recent_group.hit.mean()) if recent_n else None
        folds=[]
        for fold_index,(lo,hi) in enumerate(zip(fold_edges[:-1],fold_edges[1:]),start=1):
            fold=history_group[(history_group.tick_index>=lo)&(history_group.tick_index<hi)]
            fold_ci=wilson(int(fold.hit.sum()),len(fold)) if len(fold) else None
            folds.append({"fold":fold_index,"n":len(fold),"hit_rate":float(fold.hit.mean()) if len(fold) else None,
                          "hit_rate_ci95":list(fold_ci) if fold_ci else None,
                          "passed":bool(len(fold)>=30 and fold_ci and fold_ci[0]>.10)})
        regime_rows=[]
        for regime in ("LOW","MID","HIGH"):
            val_regime=val_group[val_group.volatility_regime==regime]
            test_regime=test_group[test_group.volatility_regime==regime]
            val_ci=wilson(int(val_regime.hit.sum()),len(val_regime)) if len(val_regime) else None
            test_ci_regime=wilson(int(test_regime.hit.sum()),len(test_regime)) if len(test_regime) else None
            total=len(val_regime)+len(test_regime)
            total_hits=int(val_regime.hit.sum())+int(test_regime.hit.sum())
            regime_rows.append({"regime":regime,"n":total,"hit_rate":total_hits/total if total else None,
                                "validation":{"n":len(val_regime),"hit_rate_ci95":list(val_ci) if val_ci else None},
                                "final_test":{"n":len(test_regime),"hit_rate_ci95":list(test_ci_regime) if test_ci_regime else None},
                                "passed":bool(len(val_regime)>=50 and val_ci and val_ci[0]>.10
                                              and len(test_regime)>=50 and test_ci_regime and test_ci_regime[0]>.10)})
        val_ev=val_summary["payout_adjusted"]
        test_ev=test_summary["payout_adjusted"] if test_summary else None
        min_quotes=min(val_ev["n_with_actual_quote"],test_ev["n_with_actual_quote"] if test_ev else 0)
        support_score=min(min_quotes/1000,1.0)*15
        val_rate=val_summary["hit_rate"]
        accuracy_score=max(0,min(1,(val_rate-.10)/.05))*15 if val_rate is not None else 0
        recent_score=max(0,min(1,(recent_hit_rate-.10)/.05))*10 if recent_hit_rate is not None and recent_n>=30 else 0
        val_ci=val_summary["hit_rate_block_bootstrap_ci95"]
        test_ci=test_summary["hit_rate_block_bootstrap_ci95"] if test_summary else None
        lower=min(val_ci[0],test_ci[0]) if val_ci and test_ci else None
        confidence_score=max(0,min(1,(lower-.10)/.05))*15 if lower is not None else 0
        calibration_values=[x["absolute_calibration_error"] for x in (val_summary,test_summary) if x and x["absolute_calibration_error"] is not None]
        calibration_error=float(np.mean(calibration_values)) if calibration_values else None
        calibration_score=max(0,1-calibration_error/.10)*15 if calibration_error is not None else 0
        walk_score=sum(x["passed"] for x in folds)/len(folds)*10
        regime_score=sum(x["passed"] for x in regime_rows)/len(regime_rows)*10
        pnl_ok=bool(val_ev.get("realized_profit_ci95") and val_ev["realized_profit_ci95"][0]>0
                    and test_ev and test_ev.get("realized_profit_ci95") and test_ev["realized_profit_ci95"][0]>0
                    and min_quotes>=1000)
        payout_score=10 if pnl_ok else 0
        health=round(support_score+accuracy_score+recent_score+confidence_score+calibration_score+walk_score+regime_score+payout_score)
        gate_reasons=[]
        if selected_strategy_id not in validation_finalists:
            gate_reasons.append("The development-selected strategy did not pass the pre-registered validation gate.")
        if test_summary is None:
            gate_reasons.append("The untouched final test is not opened for this strategy.")
        if val_summary["n"]<1000 or not test_summary or test_summary["n"]<1000:
            gate_reasons.append("Fewer than 1,000 scored predictions for this digit in validation and final test.")
        if min_quotes<1000 or val_ev["quote_coverage"]<.90 or not test_ev or test_ev["quote_coverage"]<.90:
            gate_reasons.append("Insufficient contemporaneous quoted and settled contracts for this digit.")
        if val_adjusted[digit]>=.05 or test_summary and test_summary["holm_adjusted_p_across_digits"]>=.05:
            gate_reasons.append("Digit-specific hit-rate evidence does not pass Holm correction across all ten digits.")
        if not (val_ci and val_ci[0]>.10 and test_ci and test_ci[0]>.10):
            gate_reasons.append("The block-bootstrap hit-rate lower bound is not above the 10% reference in both holdouts.")
        if not pnl_ok:
            gate_reasons.append("Positive quote-backed profit confidence bounds are not established in both holdouts.")
        if not all(x["passed"] for x in regime_rows):
            gate_reasons.append("Performance is not confirmed across all three volatility regimes.")
        output.append({
            "digit":digit,"validation":val_summary,"final_test":test_summary,
            "recent":{"n":recent_n,"hit_rate":recent_hit_rate},
            "walk_forward_folds":folds,"volatility_regimes":regime_rows,
            "health_score":health,
            "health_components":{"support":round(support_score,2),"historical_performance":round(accuracy_score,2),
                                 "recent_performance":round(recent_score,2),"uncertainty":round(confidence_score,2),
                                 "calibration":round(calibration_score,2),"walk_forward_stability":round(walk_score,2),
                                 "volatility_stability":round(regime_score,2),"payout_profitability":payout_score},
            "evidence_qualified":not gate_reasons,"gate_reasons":gate_reasons,
            "decision":"DO NOT TRADE" if gate_reasons else "PAPER TRADE CANDIDATE",
        })
    return {"selected_strategy_id":selected_strategy_id,"selection_basis":"Best multiclass Brier score on development data; no validation or final-test tuning.",
            "health_definition":"0-100 evidence-quality index from scored sample support, historical/recent hit rate, block-bootstrap uncertainty, calibration, walk-forward and volatility stability, and quote-backed profitability. It is not a win probability.",
            "minimum_per_digit_quotes_per_holdout":1000,"digits":output}

def _walk_forward(frame, n_ticks, quote_map, folds=4):
    """Four chronological diagnostic folds over development+validation only; final test is excluded."""
    cutoff=int(n_ticks*.80); edges=np.linspace(0,cutoff,folds+1,dtype=int); output=[]
    eligible=frame[frame.tick_index<cutoff]
    for i in range(folds):
        lo,hi=int(edges[i]),int(edges[i+1]); block=eligible[(eligible.tick_index>=lo)&(eligible.tick_index<hi)]
        strategies={}
        pvals=[]; ids=[]
        for sid,g in block.groupby("strategy_id"):
            summary=_summary(g,quote_map)
            ids.append(sid); pvals.append(summary["p_vs_uniform_10pct_one_sided"]); strategies[sid]=summary
        adj=holm_adjust(pvals)
        for sid,padj in zip(ids,adj): strategies[sid]["holm_adjusted_p"]=padj
        output.append({"fold":i+1,"tick_index_range_exclusive_end":[lo,hi],"strategies":strategies})
    return output

def _verdict(summary, minimum_n=5000, minimum_quoted_n=5000):
    if not summary: return "inconclusive"
    ev=summary.get("payout_adjusted") or {}
    hit_ci=summary.get("hit_rate_block_bootstrap_ci95")
    pnl_ci=ev.get("realized_profit_ci95")
    if summary.get("n",0)<minimum_n or ev.get("n_with_actual_quote",0)<minimum_quoted_n or ev.get("quote_coverage",0)<.90:
        return "inconclusive"
    if hit_ci and hit_ci[1] is not None and hit_ci[1] <= .10:
        return "no_evidence_of_edge"
    if pnl_ci and pnl_ci[1] is not None and pnl_ci[1] < 0:
        return "no_evidence_of_edge"
    if (summary.get("holm_adjusted_p",1)<.05 and hit_ci and hit_ci[0]>.10
            and pnl_ci and pnl_ci[0]>0):
        return "evidence_of_edge"
    return "inconclusive"

def evaluate_symbol(ticks: pd.DataFrame, symbol: str, stored_forecasts=None):
    if ticks.empty or len(ticks)<101:
        return {"symbol":symbol,"n_ticks":len(ticks),"status":"insufficient_data","strategies":[],"final_test_accessed":False}
    frame=_forecast_frame(ticks,symbol)
    if frame.empty: return {"symbol":symbol,"n_ticks":len(ticks),"status":"insufficient_data","strategies":[],"final_test_accessed":False}
    # Only immutable quote events captured against a particular forecast are eligible for EV.
    quote_map={}
    for r in (stored_forecasts or []):
        if r.get("actual_digit") is not None and r.get("payout") and r.get("stake") and r.get("probabilities"):
            quote_map[(r["strategy_id"],r["strategy_version"],int(r["forecast_tick_id"]))]={"payout":r["payout"],"stake":r["stake"],"probabilities":r["probabilities"],"prediction":r["prediction"]}
    dev=frame[frame.split=="development"]; val=frame[frame.split=="validation"]
    dev_rank={}
    dev_summaries={sid:_summary(g,quote_map) for sid,g in dev.groupby("strategy_id")}
    dev_ids=list(dev_summaries); dev_adj=holm_adjust([dev_summaries[s]["p_vs_uniform_10pct_one_sided"] for s in dev_ids])
    for sid,padj in zip(dev_ids,dev_adj): dev_summaries[sid]["holm_adjusted_p"]=padj
    for sid,g in dev.groupby("strategy_id"):
        dev_rank[sid]=dev_summaries[sid]["brier_multiclass_10class"]
    val_raw={sid:_summary(g,quote_map) for sid,g in val.groupby("strategy_id")}
    sids=list(val_raw); adjusted=holm_adjust([val_raw[s]["p_vs_uniform_10pct_one_sided"] for s in sids])
    for sid,padj in zip(sids,adjusted): val_raw[sid]["holm_adjusted_p"]=padj
    gates=PRE_REGISTERED["validation_gates"]; finalists=[]
    for sid in sids:
        v=val_raw[sid]; evci=v["payout_adjusted"]["realized_profit_ci95"]
        if v["n"]>=gates["minimum_n"] and v["payout_adjusted"]["n_with_actual_quote"]>=gates["minimum_quoted_n"] and v["payout_adjusted"]["quote_coverage"]>=gates["minimum_quote_coverage"] and v["holm_adjusted_p"]<gates["Holm_adjusted_one_sided_p_lt"] and v["hit_rate_block_bootstrap_ci95"] and v["hit_rate_block_bootstrap_ci95"][0]>gates["hit_rate_block_CI95_lower_gt"] and evci and evci[0]>gates["realized_profit_CI95_lower_gt"]:
            finalists.append(sid)
    test=frame[(frame.split=="final_test") & frame.strategy_id.isin(finalists)]
    test_raw={sid:_summary(g,quote_map) for sid,g in test.groupby("strategy_id")} if finalists else {}
    if test_raw:
        test_ids=list(test_raw); test_adj=holm_adjust([test_raw[s]["p_vs_uniform_10pct_one_sided"] for s in test_ids])
        for sid,padj in zip(test_ids,test_adj): test_raw[sid]["holm_adjusted_p"]=padj
    result=[]
    for sid in PRE_REGISTERED["candidate_ids"]:
        d=dev[dev.strategy_id==sid]; v=val[val.strategy_id==sid]
        validation_summary=val_raw.get(sid)
        final_summary=test_raw.get(sid)
        validation_verdict=_verdict(validation_summary,gates["minimum_n"],gates["minimum_quoted_n"])
        final_verdict=_verdict(final_summary,gates["minimum_n"],gates["minimum_quoted_n"]) if final_summary else None
        row={"strategy_id":sid,"development":dev_summaries.get(sid),"validation":validation_summary,"validation_verdict":validation_verdict,
             "final_test":final_summary,"final_test_verdict":final_verdict,
             "final_test_status":"evaluated_once_as_validation_finalist" if sid in finalists else "not_opened_not_a_validation_finalist"}
        row["verdict"]=final_verdict if final_verdict else validation_verdict
        result.append(row)
    selected_strategy_id=min(dev_rank,key=lambda sid:dev_rank[sid] if dev_rank[sid] is not None else float("inf"),default=None)
    digit_evidence=_digit_evidence(frame,quote_map,selected_strategy_id,finalists,test,test_raw,len(ticks))
    digit_counts={str(i):int((ticks.digit.astype(int)==i).sum()) for i in range(10)}
    return {"symbol":symbol,"n_ticks":len(ticks),"max_tick_id":int(ticks.id.max()),"baseline_theoretical_hit_rate":0.10,"baseline_null":"uniform independent digit guess; not asserted as market truth","empirical_digit_counts":digit_counts,"empirical_digit_frequencies":{k:v/len(ticks) for k,v in digit_counts.items()},"split_boundaries":{"development_end_tick_index":int(len(ticks)*.60),"validation_end_tick_index":int(len(ticks)*.80)},
            "development_brier_rank":dict(sorted(dev_rank.items(),key=lambda x:x[1] if x[1] is not None else float('inf'))),
            "walk_forward_development_validation_only":_walk_forward(frame,len(ticks),quote_map),
            "selected_strategy_id":selected_strategy_id,"digit_evidence":digit_evidence,
            "validation_finalists":finalists,"final_test_accessed":bool(finalists),"strategies":result,
            "limitations":["A 1-second forecast is scored only against a tick with epoch exactly forecast_epoch+1; missed epochs remain unscored.","Payout-adjusted EV uses only proposal quotes captured after that forecast and before the next tick; missing or late quotes are excluded.","Final test is not evaluated for non-finalists. Final-test results are not used to retune.","The 10% null assumes uniform independent digits; it is a theoretical reference, not a proven property of every symbol.","A validation finalist is not evidence of durable edge until the untouched test and subsequent forward paper test agree."]}

def generate_report(store, symbols):
    import json
    reports=[]
    for symbol in symbols:
        # Once any validation finalist opens the final test, freeze the entire report snapshot.
        # Subsequent API calls must reuse it and may not re-test on newly arriving ticks.
        frozen=store.research_snapshot_versioned(symbol,PROTOCOL_VERSION)
        if frozen:
            item=json.loads(frozen["report_json"])
            item["research_snapshot"]={"frozen":True,"protocol_version":PROTOCOL_VERSION,"created_utc":frozen["created_utc"],"n_ticks":frozen["n_ticks"],"max_tick_id":frozen["max_tick_id"]}
        else:
            frame=store.ticks_frame(symbol)
            item=evaluate_symbol(frame,symbol,store.forecasts(symbol))
            item.setdefault("pre_registration",PRE_REGISTERED)
            if item.get("final_test_accessed"):
                item["research_snapshot"]={"frozen":True,"protocol_version":PROTOCOL_VERSION,"created_utc":None,"n_ticks":len(frame),"max_tick_id":int(frame.id.max()) if not frame.empty else None}
                payload=json.dumps(item,allow_nan=False,default=str)
                store.freeze_research_snapshot_versioned(symbol,PROTOCOL_VERSION,len(frame),int(frame.id.max()) if not frame.empty else None,payload)
                # Re-read to report the canonical first-write snapshot in a concurrent request race.
                frozen=store.research_snapshot_versioned(symbol,PROTOCOL_VERSION)
                if frozen:
                    item=json.loads(frozen["report_json"])
                    item["research_snapshot"]={"frozen":True,"protocol_version":PROTOCOL_VERSION,"created_utc":frozen["created_utc"],"n_ticks":frozen["n_ticks"],"max_tick_id":frozen["max_tick_id"]}
        reports.append(item)
    return {"report_version":"1.0.0","pre_registration":PRE_REGISTERED,"symbols":reports}
```

## `bromax/storage.py`

```python
import json, sqlite3
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS ticks(
 id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, epoch INTEGER NOT NULL,
 quote_text TEXT NOT NULL, pip_size INTEGER NOT NULL, digit INTEGER, received_utc TEXT NOT NULL,
 source_tick_id TEXT, UNIQUE(symbol, epoch)
);
CREATE INDEX IF NOT EXISTS ix_ticks_symbol_id ON ticks(symbol,id);
CREATE TABLE IF NOT EXISTS research_snapshots(
 symbol TEXT PRIMARY KEY, created_utc TEXT NOT NULL, n_ticks INTEGER NOT NULL,
 max_tick_id INTEGER, report_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_snapshots_no_update BEFORE UPDATE ON research_snapshots BEGIN SELECT RAISE(ABORT,'research snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_snapshots_no_delete BEFORE DELETE ON research_snapshots BEGIN SELECT RAISE(ABORT,'research snapshots are immutable'); END;
CREATE TABLE IF NOT EXISTS research_snapshots_v2(
 symbol TEXT NOT NULL, protocol_version TEXT NOT NULL, created_utc TEXT NOT NULL,
 n_ticks INTEGER NOT NULL, max_tick_id INTEGER, report_json TEXT NOT NULL,
 PRIMARY KEY(symbol,protocol_version)
);
CREATE TRIGGER IF NOT EXISTS research_snapshots_v2_no_update BEFORE UPDATE ON research_snapshots_v2 BEGIN SELECT RAISE(ABORT,'versioned research snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_snapshots_v2_no_delete BEFORE DELETE ON research_snapshots_v2 BEGIN SELECT RAISE(ABORT,'versioned research snapshots are immutable'); END;
CREATE TABLE IF NOT EXISTS agent_runs(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL UNIQUE,
 created_utc TEXT NOT NULL, symbol TEXT NOT NULL, snapshot_tick_id INTEGER,
 final_status TEXT NOT NULL, decision_key TEXT NOT NULL, agent_results_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_symbol_seq ON agent_runs(symbol,seq);
CREATE TRIGGER IF NOT EXISTS agent_runs_no_update BEFORE UPDATE ON agent_runs BEGIN SELECT RAISE(ABORT,'agent runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agent_runs_no_delete BEFORE DELETE ON agent_runs BEGIN SELECT RAISE(ABORT,'agent runs are immutable'); END;
CREATE TABLE IF NOT EXISTS audit_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 event_type TEXT NOT NULL CHECK(event_type IN ('FORECAST','SCORE','QUOTE','PAPER_OPEN','PAPER_SETTLE')),
 created_utc TEXT NOT NULL, symbol TEXT NOT NULL, strategy_id TEXT NOT NULL,
 strategy_version TEXT NOT NULL, parameters_json TEXT NOT NULL,
 forecast_tick_id INTEGER, forecast_epoch INTEGER, prediction INTEGER,
 probabilities_json TEXT, payout TEXT, stake TEXT, proposal_id TEXT, quote_spot_time TEXT,
 actual_tick_id INTEGER, actual_digit INTEGER, hit INTEGER,
 paper_pnl TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_symbol_strategy ON audit_events(symbol,strategy_id,seq);
CREATE INDEX IF NOT EXISTS ix_events_type_note ON audit_events(event_type,note);
CREATE TABLE IF NOT EXISTS duration_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 event_type TEXT NOT NULL CHECK(event_type IN ('QUOTE','SETTLE')),
 created_utc TEXT NOT NULL, symbol TEXT NOT NULL, strategy_id TEXT NOT NULL,
 strategy_version TEXT NOT NULL, forecast_seq INTEGER NOT NULL, forecast_tick_id INTEGER NOT NULL,
 forecast_epoch INTEGER NOT NULL, prediction INTEGER NOT NULL, duration_ticks INTEGER NOT NULL,
 stake TEXT, payout TEXT, proposal_id TEXT, quote_spot_time TEXT,
 actual_tick_id INTEGER, actual_epoch INTEGER, actual_digit INTEGER, hit INTEGER, paper_pnl TEXT,
 UNIQUE(event_type,forecast_seq,duration_ticks)
);
CREATE INDEX IF NOT EXISTS ix_duration_symbol ON duration_events(symbol,duration_ticks,forecast_epoch);
CREATE TRIGGER IF NOT EXISTS duration_events_no_update BEFORE UPDATE ON duration_events BEGIN SELECT RAISE(ABORT,'duration_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS duration_events_no_delete BEFORE DELETE ON duration_events BEGIN SELECT RAISE(ABORT,'duration_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ticks_no_update BEFORE UPDATE ON ticks BEGIN SELECT RAISE(ABORT,'ticks is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ticks_no_delete BEFORE DELETE ON ticks BEGIN SELECT RAISE(ABORT,'ticks is append-only'); END;
"""

def utc_now(): return datetime.now(timezone.utc).isoformat()

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()

class Store:
    def __init__(self, path: str):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c: c.executescript(SCHEMA)
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection); c.row_factory = sqlite3.Row; return c
    def add_tick(self, symbol, epoch, quote_text, pip_size, digit, source_tick_id=None):
        with self.connect() as c:
            cur = c.execute("INSERT OR IGNORE INTO ticks(symbol,epoch,quote_text,pip_size,digit,received_utc,source_tick_id) VALUES(?,?,?,?,?,?,?)",
                (symbol,int(epoch),quote_text,int(pip_size),digit,utc_now(),source_tick_id))
            if cur.rowcount == 0: return None
            return cur.lastrowid
    def history(self, symbol, limit=1000):
        with self.connect() as c:
            rows = c.execute("SELECT id,epoch,digit,quote_text FROM ticks WHERE symbol=? AND digit IS NOT NULL ORDER BY epoch DESC,id DESC LIMIT ?",(symbol,limit)).fetchall()
        return list(reversed(rows))
    def digit_counts(self, symbol):
        with self.connect() as c:
            rows=c.execute("SELECT digit,COUNT(*) AS n FROM ticks WHERE symbol=? AND digit IS NOT NULL GROUP BY digit",(symbol,)).fetchall()
        counts=[0]*10
        for r in rows: counts[int(r["digit"])]=int(r["n"])
        return tuple(counts)
    def append_forecast(self, symbol, forecast, tick_id, epoch, payout=None, stake=None, proposal_id=None, note=None):
        import uuid
        with self.connect() as c:
            cur = c.execute("""INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,probabilities_json,payout,stake,proposal_id,note)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(str(uuid.uuid4()),"FORECAST",utc_now(),symbol,forecast.strategy_id,forecast.version,json.dumps(forecast.parameters,sort_keys=True),tick_id,epoch,forecast.prediction,json.dumps(forecast.probabilities),str(payout) if payout is not None else None,str(stake) if stake is not None else None,proposal_id,note))
            return cur.lastrowid
    def score_open_forecasts(self, symbol, actual_tick_id, actual_epoch, actual_digit):
        import uuid
        with self.connect() as c:
            rows = c.execute("SELECT * FROM audit_events f WHERE f.event_type='FORECAST' AND f.symbol=? AND f.forecast_epoch=? AND NOT EXISTS (SELECT 1 FROM audit_events s WHERE s.event_type='SCORE' AND s.note=CAST(f.seq AS TEXT)) ORDER BY f.seq",(symbol,int(actual_epoch)-1)).fetchall()
            scored=[]
            for f in rows:
                # 1HZ... 1-second symbols: score only the immediately following epoch.
                # If a tick was missed, leave the forecast unscored; never score it against a later tick.
                if f["forecast_tick_id"] is None or actual_tick_id <= f["forecast_tick_id"]: continue
                hit = int(f["prediction"] == actual_digit)
                c.execute("INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,actual_tick_id,actual_digit,hit,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()),"SCORE",utc_now(),symbol,f["strategy_id"],f["strategy_version"],f["parameters_json"],f["forecast_tick_id"],f["forecast_epoch"],f["prediction"],actual_tick_id,actual_digit,hit,str(f["seq"])))
                
                opens=c.execute("SELECT * FROM audit_events WHERE event_type='PAPER_OPEN' AND note=?",(str(f["seq"]),)).fetchall()
                for op in opens:
                    stake=Decimal(op["stake"]); payout=Decimal(op["payout"]); pnl=(payout-stake) if hit else -stake
                    c.execute("INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,actual_tick_id,actual_digit,hit,paper_pnl,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()),"PAPER_SETTLE",utc_now(),symbol,op["strategy_id"],op["strategy_version"],op["parameters_json"],op["forecast_tick_id"],op["forecast_epoch"],op["prediction"],actual_tick_id,actual_digit,hit,str(pnl),str(op["seq"])))
                scored.append((f["seq"],hit))
            return scored
    def append_quote(self, symbol, forecast, forecast_seq, payout, stake, proposal_id, spot_time=None):
        import uuid
        with self.connect() as c:
            c.execute("INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,payout,stake,proposal_id,quote_spot_time,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),"QUOTE",utc_now(),symbol,forecast.strategy_id,forecast.version,json.dumps(forecast.parameters,sort_keys=True),None,None,forecast.prediction,str(payout),str(stake),proposal_id,str(spot_time) if spot_time is not None else None,str(forecast_seq)))

    def append_paper_open(self, symbol, forecast, forecast_seq, tick_id, epoch, stake, payout, proposal_id, spot_time=None):
        import uuid
        with self.connect() as c:
            c.execute("INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,payout,stake,proposal_id,quote_spot_time,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),"PAPER_OPEN",utc_now(),symbol,forecast.strategy_id,forecast.version,json.dumps(forecast.parameters,sort_keys=True),tick_id,epoch,forecast.prediction,str(payout),str(stake),proposal_id,str(spot_time) if spot_time is not None else None,str(forecast_seq)))

    def is_latest_tick(self, symbol, tick_id):
        with self.connect() as c:
            row=c.execute("SELECT id FROM ticks WHERE symbol=? ORDER BY epoch DESC,id DESC LIMIT 1",(symbol,)).fetchone()
        return bool(row and row[0]==tick_id)

    def forecasts(self, symbol=None, limit=None):
        with self.connect() as c:
            q="""SELECT f.*,s.actual_digit AS scored_actual_digit,s.hit AS scored_hit,s.actual_tick_id AS scored_actual_tick_id,
            (SELECT q.payout FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS quoted_payout,
            (SELECT q.stake FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS quoted_stake,
            (SELECT q.proposal_id FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS proposal_id,
            (SELECT q.quote_spot_time FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS quoted_spot_time,
            (SELECT q.created_utc FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS quote_created_utc
            FROM audit_events f LEFT JOIN audit_events s ON s.event_type='SCORE' AND s.note=CAST(f.seq AS TEXT) WHERE f.event_type='FORECAST'"""
            args=()
            if symbol: q += " AND f.symbol=?"; args=(symbol,)
            q += " ORDER BY f.seq DESC" if limit is not None else " ORDER BY f.seq"
            if limit is not None:
                q += " LIMIT ?"
                args = args + (max(1, int(limit)),)
            result=[]
            for r in c.execute(q,args).fetchall():
                d=dict(r); d['actual_digit']=d.pop('scored_actual_digit'); d['hit']=d.pop('scored_hit'); d['actual_tick_id']=d.pop('scored_actual_tick_id'); d['quote_spot_time']=d.pop('quoted_spot_time'); d['payout']=d.pop('quoted_payout'); d['stake']=d.pop('quoted_stake'); d['probabilities']=json.loads(d['probabilities_json']) if d.get('probabilities_json') else None
                # A quote is valid for this contract only if recorded before the forecast's next 1-second epoch.
                qtime=d.get('quote_created_utc')
                if qtime:
                    from datetime import datetime
                    try: quote_epoch=datetime.fromisoformat(qtime).timestamp()
                    except ValueError: quote_epoch=float('inf')
                    spot_time=d.get('quote_spot_time')
                    try: spot_epoch=int(spot_time) if spot_time is not None else None
                    except (TypeError,ValueError): spot_epoch=None
                    if quote_epoch >= int(d['forecast_epoch'])+1 or spot_epoch != int(d['forecast_epoch']):
                        d['payout']=None; d['stake']=None; d['proposal_id']=None
                result.append(d)
            if limit is not None:
                result.reverse()
            return result

    def append_duration_quote(self, symbol, forecast, forecast_seq, tick_id, epoch, duration, stake, payout, proposal_id, spot_time):
        import uuid
        with self.connect() as c:
            # Quotes are retained only when Deriv priced the same forecast tick and
            # the quote arrived within that tick's entry window.
            import time
            if spot_time is None or int(spot_time) != int(epoch) or time.time() >= int(epoch)+1: return False
            c.execute("INSERT OR IGNORE INTO duration_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,forecast_seq,forecast_tick_id,forecast_epoch,prediction,duration_ticks,stake,payout,proposal_id,quote_spot_time) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),'QUOTE',utc_now(),symbol,forecast.strategy_id,forecast.version,int(forecast_seq),int(tick_id),int(epoch),int(forecast.prediction),int(duration),str(stake),str(payout),str(proposal_id),str(spot_time)))
            return True

    def settle_duration_contracts(self, symbol, tick_id, epoch, digit):
        import uuid
        with self.connect() as c:
            rows=c.execute("""SELECT q.* FROM duration_events q WHERE q.event_type='QUOTE' AND q.symbol=?
                AND q.forecast_epoch+q.duration_ticks=? AND NOT EXISTS
                (SELECT 1 FROM duration_events s WHERE s.event_type='SETTLE' AND s.forecast_seq=q.forecast_seq AND s.duration_ticks=q.duration_ticks)""",(symbol,int(epoch))).fetchall()
            settled=[]
            for q in rows:
                # Exact epoch equality ensures no missing-tick/look-ahead substitution.
                hit=int(int(q['prediction'])==int(digit)); stake=Decimal(q['stake']); payout=Decimal(q['payout'])
                pnl=(payout-stake) if hit else -stake
                c.execute("INSERT INTO duration_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,forecast_seq,forecast_tick_id,forecast_epoch,prediction,duration_ticks,stake,payout,proposal_id,quote_spot_time,actual_tick_id,actual_epoch,actual_digit,hit,paper_pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()),'SETTLE',utc_now(),symbol,q['strategy_id'],q['strategy_version'],q['forecast_seq'],q['forecast_tick_id'],q['forecast_epoch'],q['prediction'],q['duration_ticks'],q['stake'],q['payout'],q['proposal_id'],q['quote_spot_time'],int(tick_id),int(epoch),int(digit),hit,str(pnl)))
                settled.append({'forecast_seq':q['forecast_seq'],'duration_ticks':q['duration_ticks'],'hit':hit,'pnl':str(pnl)})
            return settled

    def duration_events(self, symbol=None):
        with self.connect() as c:
            if symbol:
                rows=c.execute("SELECT * FROM duration_events WHERE symbol=? ORDER BY seq",(symbol,)).fetchall()
            else: rows=c.execute("SELECT * FROM duration_events ORDER BY seq").fetchall()
            return [dict(r) for r in rows]

    def duration_quote(self, forecast_seq, duration_ticks):
        with self.connect() as c:
            row=c.execute("SELECT * FROM duration_events WHERE event_type='QUOTE' AND forecast_seq=? AND duration_ticks=? ORDER BY seq DESC LIMIT 1",(int(forecast_seq),int(duration_ticks))).fetchone()
        return dict(row) if row else None

    def research_snapshot(self, symbol):
        with self.connect() as c:
            row=c.execute("SELECT * FROM research_snapshots WHERE symbol=?",(symbol,)).fetchone()
        return dict(row) if row else None

    def freeze_research_snapshot(self, symbol, n_ticks, max_tick_id, report_json):
        with self.connect() as c:
            cur=c.execute("INSERT OR IGNORE INTO research_snapshots(symbol,created_utc,n_ticks,max_tick_id,report_json) VALUES(?,?,?,?,?)",
                (symbol,utc_now(),int(n_ticks),max_tick_id,report_json))
            return cur.rowcount == 1

    def research_snapshot_versioned(self, symbol, protocol_version):
        with self.connect() as c:
            row=c.execute("SELECT * FROM research_snapshots_v2 WHERE symbol=? AND protocol_version=?",(symbol,protocol_version)).fetchone()
        return dict(row) if row else None

    def freeze_research_snapshot_versioned(self, symbol, protocol_version, n_ticks, max_tick_id, report_json):
        with self.connect() as c:
            cur=c.execute("INSERT OR IGNORE INTO research_snapshots_v2(symbol,protocol_version,created_utc,n_ticks,max_tick_id,report_json) VALUES(?,?,?,?,?,?)",
                (symbol,protocol_version,utc_now(),int(n_ticks),max_tick_id,report_json))
            return cur.rowcount == 1

    def paper_events(self, symbol=None):
        with self.connect() as c:
            q="SELECT * FROM audit_events WHERE event_type IN ('PAPER_OPEN','PAPER_SETTLE')"
            args=()
            if symbol: q += " AND symbol=?"; args=(symbol,)
            q += " ORDER BY seq"
            return [dict(r) for r in c.execute(q,args).fetchall()]

    def quality_history(self, symbol, limit=20):
        with self.connect() as c:
            rows=c.execute(
                "SELECT id,epoch,quote_text,pip_size,digit,received_utc FROM ticks WHERE symbol=? ORDER BY epoch DESC,id DESC LIMIT ?",
                (symbol,max(1,int(limit))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_agent_run(self, snapshot_id, symbol, snapshot_tick_id, final_status, decision_key, agent_results, minimum_interval_seconds=60):
        now=datetime.now(timezone.utc)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            latest=c.execute(
                "SELECT created_utc,final_status,decision_key FROM agent_runs WHERE symbol=? ORDER BY seq DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if latest and latest["final_status"]==final_status and latest["decision_key"]==decision_key:
                previous=datetime.fromisoformat(latest["created_utc"])
                if (now-previous).total_seconds()<minimum_interval_seconds:
                    return False
            c.execute(
                "INSERT INTO agent_runs(snapshot_id,created_utc,symbol,snapshot_tick_id,final_status,decision_key,agent_results_json) VALUES(?,?,?,?,?,?,?)",
                (snapshot_id,now.isoformat(),symbol,snapshot_tick_id,final_status,decision_key,json.dumps(agent_results,separators=(",",":"),default=str)),
            )
            return True

    def agent_runs(self, symbol, limit=20):
        with self.connect() as c:
            rows=c.execute(
                "SELECT seq,snapshot_id,created_utc,symbol,snapshot_tick_id,final_status,decision_key,agent_results_json "
                "FROM agent_runs WHERE symbol=? ORDER BY seq DESC LIMIT ?",
                (symbol,max(1,int(limit))),
            ).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            item["agent_results"]=json.loads(item.pop("agent_results_json"))
            result.append(item)
        return result

    def ticks_frame(self, symbol):
        import pandas as pd
        with self.connect() as c:
            rows=c.execute("SELECT id,epoch,digit,quote_text FROM ticks WHERE symbol=? AND digit IS NOT NULL ORDER BY epoch,id",(symbol,)).fetchall()
        return pd.DataFrame([dict(r) for r in rows])
```

## `bromax/strategies.py`

```python
from dataclasses import dataclass
import hashlib
import math
import numpy as np

@dataclass(frozen=True)
class ForecastContext:
    symbol: str
    observed_tick_id: int
    observed_epoch: int
    history: tuple[int, ...]  # strictly past and current observed digits; never contains next tick
    cumulative_counts: tuple[int, ...] | None = None

@dataclass(frozen=True)
class Forecast:
    strategy_id: str
    version: str
    parameters: dict
    prediction: int
    probabilities: tuple[float, ...]

class Strategy:
    strategy_id = "abstract"
    version = "1.0.0"
    parameters: dict = {}
    def forecast(self, context: ForecastContext) -> Forecast:
        raise NotImplementedError

def _probabilities(counts: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    return (counts.astype(float) + alpha) / (counts.sum() + 10 * alpha)

def _make(strategy, context, probs):
    p = np.asarray(probs, dtype=float)
    if p.shape != (10,) or not np.isfinite(p).all() or (p < 0).any() or not np.isclose(p.sum(), 1.0):
        raise ValueError("Strategy must return ten valid probabilities summing to one")
    return Forecast(strategy.strategy_id, strategy.version, dict(strategy.parameters), int(np.argmax(p)), tuple(float(x) for x in p))

class UniformBaseline(Strategy):
    strategy_id = "uniform_random"
    version = "1.0.0"
    parameters = {"method": "deterministic_sha256_uniform_for_reproducibility"}
    def forecast(self, context):
        digest = hashlib.sha256(f"{context.symbol}|{context.observed_tick_id}|bromax-baseline-v1".encode()).digest()
        raw = int.from_bytes(digest[:8], "big")
        limit = (1 << 64) - ((1 << 64) % 10)
        counter = 0
        while raw >= limit:
            counter += 1
            raw = int.from_bytes(hashlib.sha256(f"{context.symbol}|{context.observed_tick_id}|bromax-baseline-v1|{counter}".encode()).digest()[:8], "big")
        digit = raw % 10
        probs = np.full(10, 0.1)
        probs[digit] = 0.1
        # The single prediction is a reproducible uniform draw; probabilities stay uniform.
        return Forecast(self.strategy_id, self.version, dict(self.parameters), digit, tuple(probs))

class CumulativeMode(Strategy):
    strategy_id = "cumulative_mode"
    version = "1.0.0"
    parameters = {"window": "all_observed_history", "tie_break": "lowest_digit"}
    def forecast(self, context):
        counts=np.asarray(context.cumulative_counts,dtype=int) if context.cumulative_counts is not None else np.bincount(context.history,minlength=10)
        return _make(self,context,_probabilities(counts,0.0) if counts.sum() else np.full(10,.1))

class RollingMode(Strategy):
    def __init__(self, window: int):
        self.window = window
        self.strategy_id = f"rolling_mode_{window}"
        self.parameters = {"window": window, "tie_break": "lowest_digit"}
    def forecast(self, context):
        h = context.history[-self.window:]
        counts = np.bincount(h, minlength=10) if h else np.zeros(10, dtype=int)
        return _make(self, context, _probabilities(counts, 0.0) if counts.sum() else np.full(10, .1))

class LaplaceFrequency(Strategy):
    def __init__(self, window: int, alpha: float = 1.0):
        self.window, self.alpha = window, alpha
        self.strategy_id = f"laplace_freq_{window}_a{alpha:g}"
        self.parameters = {"window": window, "alpha": alpha, "prior": "symmetric_dirichlet"}
    def forecast(self, context):
        h = context.history[-self.window:]
        counts = np.bincount(h, minlength=10) if h else np.zeros(10, dtype=int)
        return _make(self, context, _probabilities(counts, self.alpha))

class ExponentialFrequency(Strategy):
    def __init__(self, half_life: float):
        self.half_life = float(half_life)
        self.strategy_id = f"exp_frequency_hl{self.half_life:g}"
        self.parameters = {"half_life_ticks": self.half_life, "weight": "exp(-ln(2)*age/half_life)", "alpha": 1.0}
    def forecast(self, context):
        h = context.history[-1000:]
        if not h:
            return _make(self, context, np.full(10, .1))
        ages = np.arange(len(h) - 1, -1, -1, dtype=float)
        weights = np.exp(-math.log(2) * ages / self.half_life)
        counts = np.bincount(np.asarray(h, dtype=int), weights=weights, minlength=10)
        return _make(self, context, _probabilities(counts, 1.0))


class ConditionalTransition(Strategy):
    """Second-order Markov frequency candidate; Laplace-smoothed, fixed order."""
    strategy_id = "conditional_transition_order2"
    version = "1.0.0"
    parameters = {"order": 2, "alpha": 1.0, "minimum_support_for_interpretation": 20,
                  "low_support_fallback": "laplace_frequency_500",
                  "method": "historical_successors_of_last_two_digits"}
    def forecast(self, context):
        h = tuple(int(x) for x in context.history)
        counts = np.zeros(10, dtype=float)
        if len(h) >= 3:
            current = h[-2:]
            for j in range(2, len(h)):
                if h[j-2:j] == current:
                    counts[h[j]] += 1.0
        if counts.sum() < 20:
            fallback = np.bincount(h[-500:], minlength=10) if h else np.zeros(10, dtype=int)
            return _make(self, context, _probabilities(fallback, 1.0))
        return _make(self, context, _probabilities(counts, 1.0))

def candidate_strategies():
    """Pre-registered list. Do not edit after opening the final test split."""
    return [UniformBaseline(), CumulativeMode(), RollingMode(100), RollingMode(500), RollingMode(1000),
            LaplaceFrequency(100, 1.0), LaplaceFrequency(500, 1.0),
            ExponentialFrequency(50), ExponentialFrequency(200), ConditionalTransition()]
```

## `bromax/timing.py`

```python
"""Causal multi-horizon MATCH timing and volatility diagnostics.

This module deliberately does not authorize execution. A duration is only a
research candidate until duration-specific Deriv proposal quotes, untouched
out-of-sample validation, and forward paper settlement are available.
"""
from __future__ import annotations
from collections import defaultdict
import math
import numpy as np
from scipy.stats import binomtest, norm, t

DURATIONS = (1, 2, 3, 4, 5)
MIN_OBSERVATIONS = 5000
MIN_VOL_BUCKET_OBSERVATIONS = 1000


def wilson(k: int, n: int, z: float = 1.959963984540054):
    if n <= 0:
        return None
    p = k / n
    den = 1 + z*z/n
    center = (p + z*z/(2*n))/den
    half = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/den
    return [max(0.0, center-half), min(1.0, center+half)]


def _volatility_features(ticks):
    """Trailing 20-return mean absolute return, computed using past/current prices only."""
    prices=[]
    for row in ticks:
        try:
            prices.append(float(row['quote_text']))
        except (ValueError, TypeError, KeyError):
            prices.append(float('nan'))
    returns=[None]
    for a,b in zip(prices[:-1],prices[1:]):
        returns.append(abs((b-a)/a) if math.isfinite(a) and math.isfinite(b) and a else None)
    result=[]
    for i in range(len(ticks)):
        sample=[x for x in returns[max(1,i-19):i+1] if x is not None and math.isfinite(x)]
        result.append(float(np.mean(sample)) if len(sample)>=10 else None)
    return result


def _holm(pvalues):
    if not pvalues:
        return []
    order=sorted(range(len(pvalues)),key=lambda i:pvalues[i])
    adjusted=[1.0]*len(pvalues); running=0.0; m=len(pvalues)
    for rank,index in enumerate(order):
        running=max(running,min(1.0,(m-rank)*pvalues[index]))
        adjusted[index]=running
    return adjusted


def _summary(rows):
    n=len(rows); k=sum(x[0] for x in rows); ci=wilson(k,n)
    p=float(binomtest(k,n,.10,alternative='greater').pvalue) if n else 1.0
    return {'n':n,'hits':k,'hit_rate':k/n if n else None,'hit_rate_ci95':ci,'p_vs_10pct':p}


def evaluate_timing(ticks, forecasts, durations=DURATIONS):
    """Score frozen predictions at N ticks ahead with chronological holdouts.

    Duration candidates are selected on validation, then tested once on the
    untouched final segment. A positive hit-rate result still is not tradable
    without actual duration-specific proposal quotes and payout-adjusted EV.
    """
    ticks=[dict(x) for x in ticks]
    if not ticks:
        return {'status':'insufficient_data','symbol':None,'durations_tested_ticks':list(durations),'strategies':[],'best_duration':None,'execution_authorized':False,'limitations':['Insufficient tick history; no timing or volatility conclusion is available.']}
    by_id={int(t['id']):i for i,t in enumerate(ticks)}
    vol=_volatility_features(ticks)
    valid_train=[x for x in vol[:max(1,int(len(vol)*.60))] if x is not None]
    cuts=np.quantile(valid_train,[1/3,2/3]).tolist() if len(valid_train)>=30 else None
    samples=defaultdict(list)
    for f in forecasts:
        idx=by_id.get(int(f.get('forecast_tick_id') or -1))
        if idx is None or f.get('prediction') is None:
            continue
        for h in durations:
            target=idx+int(h)
            if target>=len(ticks) or int(ticks[target]['epoch'])-int(ticks[idx]['epoch']) != int(h):
                continue
            hit=int(int(f['prediction'])==int(ticks[target]['digit']))
            samples[(f.get('strategy_id','unknown'),int(h))].append((idx,hit,vol[idx]))
    n_ticks=len(ticks); dev_end=int(n_ticks*.60); val_end=int(n_ticks*.80)
    # Build validation family once, then adjust across every strategy-duration pair.
    validation={key:_summary([(hit,v) for idx,hit,v in rows if dev_end<=idx<val_end]) for key,rows in samples.items()}
    keys=list(validation)
    adjusted=_holm([validation[k]['p_vs_10pct'] for k in keys])
    for key,padj in zip(keys,adjusted): validation[key]['holm_adjusted_p']=padj
    gates={k for k in keys if validation[k]['n']>=MIN_OBSERVATIONS and validation[k]['hit_rate_ci95'] and validation[k]['hit_rate_ci95'][0]>.10 and validation[k]['holm_adjusted_p']<.05}
    test={key:_summary([(hit,v) for idx,hit,v in rows if idx>=val_end]) for key,rows in samples.items() if key in gates}
    test_keys=list(test); test_adjusted=_holm([test[k]['p_vs_10pct'] for k in test_keys])
    for key,padj in zip(test_keys,test_adjusted): test[key]['holm_adjusted_p']=padj
    def regime_name(value):
        if cuts is None or value is None: return None
        return 'LOW' if value<=cuts[0] else ('MID' if value<=cuts[1] else 'HIGH')
    regime_samples=defaultdict(list)
    for (sid,h),rows in samples.items():
        for idx,hit,value in rows:
            regime=regime_name(value)
            if regime: regime_samples[(sid,h,regime)].append((idx,hit,value))
    regime_validation={key:_summary([(hit,v) for idx,hit,v in rows if dev_end<=idx<val_end]) for key,rows in regime_samples.items()}
    regime_keys=list(regime_validation)
    regime_adj=_holm([regime_validation[k]['p_vs_10pct'] for k in regime_keys])
    for key,padj in zip(regime_keys,regime_adj): regime_validation[key]['holm_adjusted_p']=padj
    regime_gates={key for key,summary in regime_validation.items()
                  if summary['n']>=MIN_VOL_BUCKET_OBSERVATIONS and summary['hit_rate_ci95']
                  and summary['hit_rate_ci95'][0]>.10 and summary['holm_adjusted_p']<.05}
    regime_test={key:_summary([(hit,v) for idx,hit,v in rows if idx>=val_end])
                 for key,rows in regime_samples.items() if key in regime_gates}
    regime_test_keys=list(regime_test)
    regime_test_adj=_holm([regime_test[k]['p_vs_10pct'] for k in regime_test_keys])
    for key,padj in zip(regime_test_keys,regime_test_adj): regime_test[key]['holm_adjusted_p']=padj
    strategies=[]
    for (sid,h),rows in sorted(samples.items()):
        all_hits=[(hit,v) for _,hit,v in rows]
        summary=_summary(all_hits); val=validation.get((sid,h),_summary([])); final=test.get((sid,h))
        buckets=[]
        for name in ('LOW','MID','HIGH'):
            key=(sid,h,name); bucket_rows=regime_samples.get(key,[])
            bv=regime_validation.get(key,_summary([])); bt=regime_test.get(key)
            confirmed=bool(bv['n']>=MIN_VOL_BUCKET_OBSERVATIONS and bv['hit_rate_ci95'] and bv['hit_rate_ci95'][0]>.10 and bv.get('holm_adjusted_p',1)<.05 and bt and bt['n']>=MIN_VOL_BUCKET_OBSERVATIONS and bt['hit_rate_ci95'] and bt['hit_rate_ci95'][0]>.10 and bt['holm_adjusted_p']<.05)
            buckets.append({'regime':name,'n':len(bucket_rows),'hits':sum(x[1] for x in bucket_rows),
                            'validation':bv,'final_test':bt,
                            'status':'CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON' if confirmed else 'UNCONFIRMED'})
        final_ok=bool(final and final['n']>=MIN_OBSERVATIONS and final['hit_rate_ci95'] and final['hit_rate_ci95'][0]>.10 and final['holm_adjusted_p']<.05)
        strategies.append({'strategy_id':sid,'duration_ticks':h,**summary,
                           'development':_summary([(hit,v) for idx,hit,v in rows if idx<dev_end]),
                           'validation':val,'final_test':final,
                           'timing_status':'HIT_RATE_EDGE_ONLY_REQUIRES_QUOTE_EV' if final_ok else 'RESEARCH_ONLY',
                           'volatility_regimes':buckets})
    eligible=[x for x in strategies if x['timing_status']=='HIT_RATE_EDGE_ONLY_REQUIRES_QUOTE_EV']
    best=max(eligible,key=lambda x:(x['final_test']['hit_rate_ci95'][0],x['final_test']['hit_rate']),default=None)
    valid_vol=[x for x in vol if x is not None]
    current_volatility=vol[-1] if vol else None
    short_volatility=float(np.mean(valid_vol[-20:])) if valid_vol else None
    long_volatility=float(np.mean(valid_vol[-200:])) if valid_vol else None
    return {'status':'research_only','symbol':ticks[-1].get('symbol'),
            'durations_tested_ticks':list(durations),'minimum_observations_per_holdout':MIN_OBSERVATIONS,
            'chronological_splits':{'development':'first 60%','validation':'next 20%','final_test':'last 20%'},
            'volatility_cutoffs_training_terciles':cuts,'current_volatility_regime':regime_name(vol[-1] if vol else None),'strategies':strategies,
            'current_volatility':current_volatility,'short_term_volatility':short_volatility,
            'long_term_volatility':long_volatility,
            'best_research_candidate':best,'best_duration':best['duration_ticks'] if best else None,
            'execution_authorized':False,
            'limitations':['Predictions are frozen per-tick strategy forecasts scored at future horizons; models are not separately retrained for each duration.',
              'Final-test selection uses hit-rate significance only; no duration-specific historical Deriv proposal quotes are stored, so payout-adjusted expected value is unverified.',
              'Volatility confirmation is strategy- and horizon-specific, requires at least 1,000 untouched final-test observations per regime and Holm-adjusted significance; even then it is not proof of a correct next digit.',
              'A duration cannot be enabled until its own quote capture, payout-adjusted EV, untouched test and forward paper settlement pass.',
              'The existing 1-tick paper executor remains unchanged. No live orders are possible.']}


def _profit_summary(rows):
    """Quote-backed gross-return P&L summary; each row has stake and payout."""
    values=[]
    for row in rows:
        try:
            stake=float(row['stake']); payout=float(row['payout']); hit=int(row['hit'])
            if stake>0 and math.isfinite(stake) and math.isfinite(payout):
                pnl=(payout-stake) if hit else -stake
                values.append((pnl,stake,pnl/stake))
        except (TypeError,ValueError,KeyError):
            continue
    n=len(values)
    if not n: return {'n':0,'wins':0,'hit_rate':None,'total_stake':0.0,'total_pnl':0.0,'mean_pnl_per_contract':None,'mean_return_on_stake':None,'mean_return_ci95':None,'p_value_profit_gt_zero':1.0}
    returns=np.asarray([x[2] for x in values],dtype=float)
    pnls=np.asarray([x[0] for x in values],dtype=float)
    if n>1 and np.std(returns,ddof=1)>0:
        # Newey-West/HAC standard error: overlapping 1-5 tick contracts and
        # serial dependence otherwise make the naive IID interval too narrow.
        centered=returns-float(np.mean(returns)); gamma0=float(np.dot(centered,centered)/n)
        horizon=max(1,int(rows[0].get('duration_ticks',1)))
        lag=5
        for k in range(1,lag+1):
            gamma=float(np.dot(centered[k:],centered[:-k])/n)
            gamma0 += 2.0*(1.0-k/(lag+1))*gamma
        se=float(math.sqrt(max(gamma0,0.0)/n)); crit=float(norm.ppf(.975))
        ci=[float(np.mean(returns)-crit*se),float(np.mean(returns)+crit*se)] if se>0 else None
        p=float(norm.sf(float(np.mean(returns))/se)) if se>0 else 1.0
    else:
        ci=None; p=1.0
    return {'n':n,'wins':sum(x[0]>0 for x in values),'hit_rate':sum(x[0]>0 for x in values)/n,
            'total_stake':float(sum(x[1] for x in values)),'total_pnl':float(np.sum(pnls)),
            'mean_pnl_per_contract':float(np.mean(pnls)),'mean_return_on_stake':float(np.mean(returns)),
            'mean_return_ci95':ci,'p_value_profit_gt_zero':p}


def evaluate_payout_timing(duration_events, min_holdout=5000):
    """Select a duration only from real, contemporaneous Deriv quotes and settled outcomes.

    Quotes are frozen before their target tick; contracts are settled only at the
    exact target epoch. Validation selects candidates; final test is untouched.
    """
    quotes={}
    settled={}
    for row in duration_events:
        key=(int(row.get('forecast_seq') or 0),int(row.get('duration_ticks') or 0))
        if row.get('event_type')=='QUOTE': quotes[key]=row
        elif row.get('event_type')=='SETTLE': settled[key]=row
    pairs=[]
    for key,q in quotes.items():
        outcome=settled.get(key)
        if not outcome: continue
        # Never use a quote with a mismatched spot tick or an outcome at wrong epoch.
        if str(q.get('quote_spot_time')) != str(q.get('forecast_epoch')): continue
        if int(outcome.get('actual_epoch') or -1) != int(q['forecast_epoch'])+int(q['duration_ticks']): continue
        pairs.append({**q,**outcome,'event_type':'SETTLED'})
    if not pairs:
        return {'status':'collecting_quote_backed_settlements','durations_tested_ticks':[1,2,3,4,5],
                'minimum_observations_per_holdout':min_holdout,'strategies':[],'best_duration':None,
                'best_candidate':None,'execution_authorized':False,
                'note':'No complete duration-specific quote and settlement pairs yet. Quotes and outcomes must accrue prospectively; historical prices cannot recreate past Deriv payouts.'}
    pairs.sort(key=lambda x:(int(x['forecast_epoch']),int(x['forecast_seq'])))
    epochs=sorted({int(x['forecast_epoch']) for x in pairs})
    cut1=epochs[int(len(epochs)*.60)] if len(epochs)>2 else 10**30
    cut2=epochs[int(len(epochs)*.80)] if len(epochs)>2 else 10**30
    grouped={}
    for row in pairs: grouped.setdefault((row['strategy_id'],int(row['duration_ticks'])),[]).append(row)
    validation={k:_profit_summary([r for r in rows if cut1<=int(r['forecast_epoch'])<cut2]) for k,rows in grouped.items()}
    keys=list(validation); adjusted=_holm([validation[k]['p_value_profit_gt_zero'] for k in keys])
    for k,padj in zip(keys,adjusted): validation[k]['holm_adjusted_p']=padj
    finalists={k for k,v in validation.items() if v['n']>=min_holdout and v['mean_return_ci95'] and v['mean_return_ci95'][0]>0 and v['holm_adjusted_p']<.05}
    final={k:_profit_summary([r for r in rows if int(r['forecast_epoch'])>=cut2]) for k,rows in grouped.items() if k in finalists}
    fkeys=list(final); fadj=_holm([final[k]['p_value_profit_gt_zero'] for k in fkeys])
    for k,padj in zip(fkeys,fadj): final[k]['holm_adjusted_p']=padj
    output=[]
    for (sid,h),rows in sorted(grouped.items()):
        dev=_profit_summary([r for r in rows if int(r['forecast_epoch'])<cut1])
        val=validation[(sid,h)]; test=final.get((sid,h))
        passed=bool(test and test['n']>=min_holdout and test['mean_return_ci95'] and test['mean_return_ci95'][0]>0 and test.get('holm_adjusted_p',1)<.05)
        output.append({'strategy_id':sid,'duration_ticks':h,'all_settled':_profit_summary(rows),
                       'development':dev,'validation':val,'final_test':test,
                       'payout_ev_status':'OUT_OF_SAMPLE_PROFIT_EVIDENCE' if passed else 'RESEARCH_ONLY'})
    eligible=[x for x in output if x['payout_ev_status']=='OUT_OF_SAMPLE_PROFIT_EVIDENCE']
    best=max(eligible,key=lambda x:x['final_test']['mean_return_ci95'][0],default=None)
    return {'status':'quote_backed_research','durations_tested_ticks':[1,2,3,4,5],
            'minimum_observations_per_holdout':min_holdout,
            'chronological_splits':{'development':'first 60%','validation':'next 20%','final_test':'last 20%'},
            'quote_settled_pairs':len(pairs),'strategies':output,'best_candidate':best,
            'best_duration':best['duration_ticks'] if best else None,
            'execution_authorized':False,
            'limitations':['Expected value is calculated from actual quoted gross payout and stake, then realized against the exact future digit.','Only prospective quote-and-settlement pairs count; historical tick data cannot reconstruct past proposal prices.','A positive result is historical evidence, not a guarantee. Forward paper operation and risk approval remain mandatory.','No live contract purchase is enabled.']}
```

## `CHANGELOG.md`

```markdown
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
```

## `Dockerfile`

```text
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app.py ./
COPY bromax ./bromax

RUN pip install --no-cache-dir . \
    && mkdir -p /data

ENV DATABASE_PATH=/data/bromax.sqlite3 \
    BROMAX_REQUIRE_AUTH=true

EXPOSE 8765

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8765}"]
```

## `docs/API_AND_STATISTICS.md`

```markdown
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

`GET /dashboard` is the primary compact screen. The dashboard and Windows floating panel both consume `GET /decision?symbol=...`; they do not independently combine `/digits/analysis` and `/signals/ready` responses. Each response has a snapshot ID and timestamp, is tied to one latest tick ID, and is vetoed if the latest tick changes while the inputs are assembled. The response includes the ten calculated digit probabilities and evidence-health scores, rejection reasons, the coordinator's signal, current market and volatility status, actual paper-ledger outcomes, and all eight agent summaries. Missing estimates remain `null`; no percentages are substituted.

The previous detailed dashboard is preserved at `GET /advanced`. The floating Windows panel shows only the same recommended digit, probability, health, duration, volatility approval, and paper-only trade status.

### Live market pipeline status and signal history

`GET /health` reports the Deriv WebSocket connection separately from per-symbol subscription and data receipt. Each symbol includes received and processed tick counts, duplicate/invalid/out-of-order counts, observed epoch gaps, last tick and freshness, analysis/strategy/signal-generation state, reconnect count, stored tick/forecast totals, and the most recent processing error. A connected socket is not treated as proof that ticks or forecasts are flowing.

The dashboard displays these pipeline stages and polls `GET /signals/history?symbol=...&limit=10`. That endpoint returns persisted forecasts with strategy, predicted digit, model probability, target epoch, score, and status (`CURRENT`, `SCORED_HIT`, `SCORED_MISS`, or `EXPIRED_UNSCORED`). `CURRENT` is reported only when a forecast uses the latest observed tick and the live feed is fresh. The history is analysis-only; it is not an order request or trading authorization.

The collector uses WebSocket ping/pong, retries network and normal stream closures with bounded exponential backoff, re-seeds historical context, and re-subscribes after reconnecting. Historical backfill may add older records to the append-only database; storage queries sort by market epoch so insertion order cannot corrupt analysis history or latest-tick checks. Seed ticks never create retrospective forecasts. Live epoch gaps are counted, duplicate epochs are ignored, and out-of-order live ticks are dropped. The existing one-second `1HZ` scoring protocol and strategy rules are unchanged.

### Live-execution kill switch foundation

The current release has no live order implementation. `ExecutionInterface` remains disabled, starts with its kill switch engaged, and checks that state before rejecting execution. `POST /safety/kill-switch` engages the process-wide switch and offers no API to clear it; a process restart also starts engaged. This is a fail-closed foundation, not a substitute for testing an actual executor or a broker-side stop. Keep the service bound to localhost unless strong authentication and transport security are configured.

### Deterministic specialist coordination

`bromax/agents.py` defines typed, deterministic specialists for data quality, statistical strategy research, probability/calibration integrity, market regime, quote-backed contract economics, independent risk approval, and performance monitoring, plus the chief coordinator. These are ordinary Python functions over existing report, quote, tick, and paper-ledger facts—not LLM-generated forecasts. Optional LLM research is disabled and cannot authorize a trade.

The risk specialist requires the unchanged existing statistical-finalist and untouched-test checks, a complete ten-digit probability vector, fresh contiguous precision-verified ticks, a passing strategy-specific volatility filter, an unexpired matching proposal, positive recomputed EV, explicit strategy approval, paper-only mode, and per-digit qualification. The chief coordinator also vetoes an inconsistent snapshot. Missing evidence returns `NO TRADE — INSUFFICIENT EVIDENCE`; failed validation returns `REJECTED — FAILED VALIDATION`.

Agent outputs are persisted to the append-only SQLite `agent_runs` ledger. Identical final status and signal snapshots are throttled to one record per symbol per minute; a changed signal identity is recorded immediately. The ledger has update/delete guards. The dashboard and panel render the same endpoint shape; their polling intervals may differ, but each individual display is internally consistent to one snapshot. This endpoint remains paper-only and does not add order execution.
```

## `INSTALL_AND_RUN.bat`

```bat
@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11 or newer required'"
  if errorlevel 1 goto error
  py -3 -m venv .venv
  if errorlevel 1 goto error
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto error
pip install -e ".[test]"
if errorlevel 1 goto error
if not exist .env copy .env.example .env
start "BROMAX Server" cmd /k python -m uvicorn app:app --host 127.0.0.1 --port 8765
timeout /t 3 /nobreak >nul
start "BROMAX Floating Panel" cmd /k python -m bromax.desktop
start "" http://127.0.0.1:8765/dashboard
goto end
:error
echo BROMAX setup failed. Review the error above and check Python 3.11+ is installed.
pause
:end
endlocal
```

## `pyproject.toml`

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "bromax"
version = "2.2.0"
description = "Auditable, paper-only Deriv MATCH research system"
requires-python = ">=3.11"
dependencies = ["fastapi>=0.115,<1", "uvicorn[standard]>=0.34,<1", "websockets>=15,<17", "numpy>=2,<3", "pandas>=2.2,<4", "scipy>=1.14,<2", "pydantic>=2.9,<3", "python-dotenv>=1,<2"]

[project.scripts]
bromax-panel = "bromax.desktop:main"

[project.optional-dependencies]
test = ["pytest>=8,<10", "pytest-asyncio>=0.24,<2", "httpx>=0.27,<1"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.setuptools.packages.find]
include = ["bromax*"]
exclude = ["tests*", "data*", "reports*"]

[tool.setuptools.package-data]
bromax = ["dashboard.html", "dashboard_minimal.html"]
```

## `README.md`

````markdown
# BROMAX 2.0.0 — Deriv MATCH research, signal desk and paper bot

**Safety boundary:** BROMAX is paper-only. It connects to public market data and requests read-only price proposals. It has no API-token handling, no account authentication, and no `buy`, `sell`, or live order-placement implementation. No live-execution endpoint is exposed.

BROMAX 1.1.0 is a research system, not a promise of profit. A strategy is not approved merely because it wins often. It must pass a pre-registered validation gate using the actual quoted payout and then survive an untouched final test. The default approval file is absent, so the paper bot does not open any paper positions until a strategy has been explicitly approved by the research process.

## 1. Intelligence-to-bot integration (2.0.0)

BROMAX is one connected application, not two independent predictors. The Deriv collector feeds the intelligence engine; the engine freezes forecasts before the next tick; the proposal client requests a read-only quote for the exact predicted digit; the paper bot receives that same forecast, quote, and forecast ID. It checks explicit strategy approval, frozen research evidence, payout-adjusted expected value, quote timing, current tick identity, stake ceiling, daily loss cap, and trade-count limit before opening a simulated position. The next tick settles that exact forecast. The `/bridge/status` endpoint exposes the handoff state. The dashboard/API can monitor it at `/bridge/status`.

**No live trading:** this build intentionally contains no API token login, no account authorization, and no `buy`/`sell` request. It is fully runnable in live-feed + read-only-quote + paper-execution mode. Because no strategy has been validated and explicitly approved by default, a fresh installation starts in observation/research mode and does not fabricate trades.

The minimal dashboard and Windows floating panel both consume `GET /decision?symbol=...`. This single deterministic snapshot runs eight specialist roles (including the coordinator), checks that the market tick did not change during evaluation, and exposes a paper signal only when the existing research, probability, quote, volatility, approval, and risk gates all pass. Agent summaries are stored append-only and repeated identical decisions are throttled; an LLM is not needed to calculate probabilities or authorize trades. The primary dashboard also shows connection, subscription, tick-processing, strategy, signal-generation, and persisted forecast-history status separately.

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

- `GET /dashboard` — compact trading decision terminal with live pipeline status and persisted signal history.
- `GET /advanced` — detailed research, strategy, timing, and historical dashboard.
- `GET /decision?symbol=1HZ100V` — one timestamped response for the dashboard and floating panel, including all ten digit decisions, market freshness, per-symbol collector/analysis pipeline state, volatility, recorded paper results, and the eight deterministic agent results. It fails closed if the latest tick changes during evaluation.
- `POST /safety/kill-switch` — one-way, process-wide execution stop. It starts engaged, can only be engaged (never cleared through the API), and does not add live execution.
- `GET /health` — WebSocket and subscription state, per-symbol tick/forecast counts, last quote, data freshness, analysis/strategy/signal-generation state, quality counters, and recent errors.
- `GET /ticks/latest?symbol=1HZ100V` — latest raw/displayed quotes and digits.
- `GET /digits/distribution?symbol=1HZ100V&window=1000` — rolling empirical digit frequencies.
- `GET /intelligence?symbol=1HZ100V&window=1000` — conditional order 1/2/3 recurrence, transition matrix, digit-run length, uniformity diagnostic and descriptive price-volatility metrics.
- `GET /signals?symbol=1HZ100V` — current candidate forecasts and probability vectors, always marked research-only and never an approval to trade.
- `GET /signals/history?symbol=1HZ100V&limit=50` — persisted strategy forecasts with prediction probability, target epoch, scored hit/miss, and current/expired status. This read-only analysis history never places an order.
- `GET /forecasts?symbol=1HZ100V&limit=500` — most recent forecasts, scores and quote metadata (limit 1–5,000).
- `GET /report` — statistical report.
- `GET /paper/status` — paper limits and execution state.
- `GET /paper/trades?symbol=1HZ100V` — append-only paper open/settlement events.

There is no live-execution route and no buy/sell endpoint.

The collector stores data in `data/bromax.sqlite3`. Research final-test snapshots are stored write-once in the same database; new ticks after the frozen snapshot belong to the forward paper period, not a refreshed final test. Keep the process running to collect a meaningful sample. A live quote proposal is requested asynchronously for each distinct predicted digit (maximum three concurrent proposal connections and 20 pending quote tasks; if the queue fills, forecasts are retained but payout requests are skipped). The proposal `spot_time` must equal the forecast tick epoch, and the quote must be recorded before the next tick; otherwise no paper position is opened and the payout is excluded. The forecast remains in the log with missing payout and is excluded from payout-adjusted EV.

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

`/dashboard` is the compact decision terminal: market status, a ten-digit probability/health/decision table, the best qualified paper signal, volatility-filter status, and recorded paper results. Probabilities remain blank when the model has no estimate. The 0–100 evidence-health score is not a win probability, and no digit is marked `TRADE · PAPER ONLY` unless per-digit research evidence and every current quote, timing, manual-approval, and paper-risk gate pass. The former detailed dashboard remains available at `/advanced`; `/digits/analysis?symbol=1HZ100V` supplies its full research data to clients.

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
````

## `requirements-dev.txt`

```text
-r requirements.txt
pytest>=8,<10
pytest-asyncio>=0.24,<2
httpx>=0.27,<1
```

## `requirements.txt`

```text
fastapi>=0.115,<1
uvicorn[standard]>=0.34,<1
websockets>=15,<17
numpy>=2,<3
pandas>=2.2,<4
scipy>=1.14,<2
pydantic>=2.9,<3
python-dotenv>=1,<2
```

## `RUN_TESTS.bat`

```bat
@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Run INSTALL_AND_RUN.bat first to create the environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
pytest -q
pause
endlocal
```

## `tests/test_agents.py`

```python
from datetime import datetime, timedelta, timezone

from bromax.agents import (
    AgentResult,
    ChiefIntelligenceCoordinator,
    DataIntelligenceAgent,
    PerformanceLearningAgent,
    ProbabilityCalibrationAgent,
)
from bromax.storage import Store


def _ticks(count=3):
    received = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    return [
        {
            "id": index + 1,
            "epoch": 100 + index,
            "quote_text": f"1.{20 + index:02d}",
            "pip_size": 2,
            "digit": index,
            "received_utc": received,
        }
        for index in range(count)
    ]


def test_data_agent_approves_only_fresh_contiguous_precision_checked_ticks():
    result = DataIntelligenceAgent().run(_ticks(), False, 5)
    assert result.status == "APPROVED"
    assert result.metrics["quality_score"] == 100

    malformed = _ticks()
    malformed[-1]["digit"] = 9
    result = DataIntelligenceAgent().run(malformed, False, 5)
    assert result.status == "REJECTED"
    assert "quote" in result.reasons[0].lower() or "missing tick" in result.reasons[0].lower()


def test_data_agent_rejects_stale_feed_even_if_stored_ticks_look_valid():
    result = DataIntelligenceAgent().run(_ticks(), True, 5)
    assert result.status == "REJECTED"
    assert result.metrics["checks"]["fresh"] is False


def test_probability_agent_keeps_missing_estimates_missing():
    analysis = {"digits":[
        {"digit":digit,"estimated_win_probability":None,"health_percent":None}
        for digit in range(10)
    ]}
    result = ProbabilityCalibrationAgent().run(analysis,{"signals":[]})
    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert result.metrics["valid_digit_estimates"] == 0


def test_performance_agent_reports_only_recorded_paper_outcomes():
    result = PerformanceLearningAgent().run([],[],[])
    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert result.metrics["paper_opened"] == 0
    assert result.metrics["win_rate"] is None
    assert result.metrics["net_paper_profit"] is None


def test_agent_run_persistence_is_immutable_and_throttled_by_decision(tmp_path):
    store = Store(str(tmp_path / "agent-runs.sqlite3"))
    agents = [{"agent_id": "chief_intelligence_coordinator", "status": "INSUFFICIENT_EVIDENCE"}]
    assert store.record_agent_run("snapshot-1", "1HZ100V", 7, "NO TRADE", "NO TRADE", agents)
    assert not store.record_agent_run("snapshot-2", "1HZ100V", 7, "NO TRADE", "NO TRADE", agents)
    assert store.record_agent_run("snapshot-3", "1HZ100V", 7, "NO TRADE", "NO TRADE|digit=4", agents)
    runs = store.agent_runs("1HZ100V")
    assert [run["snapshot_id"] for run in runs] == ["snapshot-3", "snapshot-1"]
    assert runs[0]["agent_results"] == agents
    with store.connect() as connection:
        try:
            connection.execute("UPDATE agent_runs SET final_status='TRADE'")
        except Exception:
            pass
        else:
            raise AssertionError("agent run records must be immutable")


def test_coordinator_vetoes_even_an_approved_specialist_result_on_mixed_snapshot():
    risk = AgentResult(
        agent_id="risk_signal_approval",
        status="APPROVED",
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=1,
        summary="approved",
        reasons=(),
        metrics={},
        output={"trade_authorized": True, "signal": {"predicted_digit": 2}},
    )
    result = ChiefIntelligenceCoordinator().coordinate([], risk, consistent=False)
    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert result.output["trade_authorized"] is False
    assert result.output["final_status"] == "NO TRADE — INSUFFICIENT EVIDENCE"
```

## `tests/test_api.py`

```python
import pytest

def test_api_exposes_research_and_paper_routes_but_no_live_order_route(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"api.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    paths={route.path for route in api_module.app.routes}
    assert "/health" in paths
    assert "/safety/kill-switch" in paths
    assert "/report" in paths
    assert "/paper/trades" in paths
    assert "/signals/history" in paths
    assert "/intelligence" in paths
    assert "/signals" in paths
    assert "/signals/ready" in paths
    assert "/decision" in paths
    assert "/timing" in paths
    assert "/bridge/status" in paths
    assert "/dashboard" in paths
    assert "/advanced" in paths
    assert "/digits/analysis" in paths
    assert not any("buy" in path.lower() or "live" in path.lower() for path in paths)
    assert api_module.paper_status()["mode"]=="SIMULATED_ONLY"
    assert api_module.execution_interface.kill_switch_engaged is True
    switch=api_module.engage_execution_kill_switch()
    assert switch["status"]=="KILL_SWITCH_ENGAGED"
    assert switch["live_execution_implemented"] is False
    assert switch["live_execution_enabled"] is False
    bridge=api_module.bridge_status()
    assert bridge["execution"]=="paper_only_no_buy_endpoint"
    assert bridge["mode"]=="SIMULATED_ONLY"
    page=api_module.dashboard()
    assert "BROMAX" in page
    assert "Best trading signal" in page
    assert "NO QUALIFIED SIGNAL" in page
    assert "Digit prediction" in page
    assert "Live analysis pipeline" in page
    assert "signalHistory" in page
    assert page.count("<section ")==6
    assert "/decision?symbol=" in page
    assert "/digits/analysis?symbol=" not in page
    assert "Timing & volatility research" not in page
    advanced=api_module.advanced_dashboard()
    assert "Ready to trade" in advanced
    assert 'href="/dashboard"' in advanced
    ready=api_module.ready_signal("1HZ100V")
    assert ready["eligible"] is False
    assert ready["status"] == "WAITING"
    health=api_module.health()
    assert health["pipeline"]["1HZ100V"]["connection_status"]=="DISCONNECTED"
    signal_history=api_module.signal_history("1HZ100V")
    assert signal_history["trade_authorized"] is False
    assert signal_history["signals"]==[]
    assert api_module.intelligence("1HZ100V",100)["signal_status"]=="RESEARCH_ONLY_NOT_APPROVED"
    assert api_module.signals("1HZ100V")["trade_authorized"] is False
    timing = api_module.timing_research("1HZ100V")
    assert timing["execution_authorized"] is False
    assert timing["durations_tested_ticks"] == [1, 2, 3, 4, 5]
    assert "payout_optimization" in timing
    assert timing["payout_optimization"]["execution_authorized"] is False


def test_unified_decision_returns_one_persisted_no_trade_snapshot(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"decision.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    monkeypatch.setattr(api_module,"report",lambda:{"symbols":[{"symbol":"1HZ100V","digit_evidence":{}}]})
    monkeypatch.setattr(api_module,"digit_analysis",lambda symbol:{
        "latest_tick":None,"volatility_filter":{"status":"INSUFFICIENT DATA"},
        "digits":[{"digit":digit,"estimated_win_probability":None,"health_percent":None,
                   "decision":"DO NOT TRADE","reasons":["Insufficient validated evidence."]}
                  for digit in range(10)],
    })
    monkeypatch.setattr(api_module,"signals",lambda symbol:{"signals":[],"trade_authorized":False})
    monkeypatch.setattr(api_module,"timing_research",lambda symbol:{"strategies":[]})
    monkeypatch.setattr(api_module,"ready_signal",lambda symbol:{
        "symbol":symbol,"status":"WAITING","eligible":False,"signal":None,
        "reason":"No statistically approved quote-backed signal.",
    })

    first=api_module.unified_decision("1HZ100V")
    second=api_module.unified_decision("1HZ100V")
    assert first["trade_authorized"] is False
    assert first["final_status"]=="NO TRADE — INSUFFICIENT EVIDENCE"
    assert len(first["digits"])==10
    assert len(first["agents"])==8
    assert first["agent_run_persisted"] is True
    assert second["agent_run_persisted"] is False
    assert len(api_module.store.agent_runs("1HZ100V"))==1
    assert {agent["evaluated_at"] for agent in first["agents"]}=={first["created_at"]}

def test_digit_analysis_returns_all_digits_without_inventing_estimates(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"digit-analysis.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    monkeypatch.setattr(api_module,"report",lambda:{"symbols":[{"symbol":"1HZ100V","digit_evidence":{"selected_strategy_id":None,"digits":[]}}]})
    monkeypatch.setattr(api_module,"signals",lambda symbol:{"signals":[]})
    monkeypatch.setattr(api_module,"timing_research",lambda symbol:{"strategies":[],"payout_optimization":{}})
    result=api_module.digit_analysis("1HZ100V")
    assert [row["digit"] for row in result["digits"]]==list(range(10))
    assert result["trade_authorized"] is False
    assert all(row["estimated_win_probability"] is None for row in result["digits"])
    assert all(row["health_percent"] is None for row in result["digits"])
    assert all(row["decision"]=="DO NOT TRADE" for row in result["digits"])
    assert result["volatility_filter"]["health_percent"] is None


def test_signal_history_marks_current_and_expired_persisted_forecasts(monkeypatch,tmp_path):
    import importlib
    import time
    from bromax.strategies import Forecast

    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"signal-history.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    monkeypatch.setenv("MAX_DATA_AGE_SECONDS","5")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    tick_id=api_module.store.add_tick("1HZ100V",1700000000,"1234.56",2,6)
    forecast=Forecast("history-test","test",{},3,(.1,.1,.1,.4,.1,.05,.05,.05,.025,.025))
    api_module.store.append_forecast("1HZ100V",forecast,tick_id,1700000000)
    api_module.collector.last_received["1HZ100V"]=time.time()

    result=api_module.signal_history("1HZ100V")
    assert result["trade_authorized"] is False
    assert result["signals"][0]["prediction"]==3
    assert result["signals"][0]["confidence"]==.4
    assert result["signals"][0]["target_epoch"]==1700000001
    assert result["signals"][0]["status"]=="CURRENT"

    api_module.collector.last_received["1HZ100V"]=time.time()-10
    expired=api_module.signal_history("1HZ100V")
    assert expired["signals"][0]["status"]=="EXPIRED_UNSCORED"


@pytest.mark.asyncio
async def test_kill_switch_endpoint_engages_and_never_enables_live_execution(monkeypatch, tmp_path):
    import importlib
    import httpx

    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"kill-switch.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    transport=httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(transport=transport,base_url="http://test") as client:
        response=await client.post("/safety/kill-switch")
    assert response.status_code==200
    result=response.json()
    assert result["kill_switch_engaged"] is True
    assert result["live_execution_implemented"] is False
    assert result["live_execution_enabled"] is False


def test_volatility_health_uses_observed_holdout_checks(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"volatility-health.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    monkeypatch.setattr(api_module,"report",lambda:{"symbols":[{"symbol":"1HZ100V","digit_evidence":{"selected_strategy_id":"candidate","digits":[]}}]})
    monkeypatch.setattr(api_module,"signals",lambda symbol:{"signals":[{
        "strategy_id":"candidate","version":"1.0","predicted_digit":0,
        "predicted_digit_probability":.1,"probabilities":[.1]*10,
    }]})
    holdout={"n":1000,"hit_rate_ci95":[.11,.14],"holm_adjusted_p":.01}
    monkeypatch.setattr(api_module,"timing_research",lambda symbol:{
        "current_volatility_regime":"LOW",
        "strategies":[{"strategy_id":"candidate","duration_ticks":1,"volatility_regimes":[
            {"regime":"LOW","status":"CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON","validation":holdout,"final_test":holdout}
        ]}],
        "payout_optimization":{},
    })
    result=api_module.digit_analysis("1HZ100V")
    assert result["volatility_filter"]["status"]=="PASS"
    assert result["volatility_filter"]["health_percent"]==100
    assert result["volatility_filter"]["checks_passed"]==6

@pytest.mark.asyncio
async def test_cloud_basic_auth_protects_dashboard_and_api(monkeypatch, tmp_path):
    import base64
    import importlib
    import httpx

    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"protected.sqlite3"))
    monkeypatch.setenv("BROMAX_REQUIRE_AUTH","true")
    monkeypatch.setenv("BROMAX_AUTH_USERNAME","viewer")
    monkeypatch.setenv("BROMAX_AUTH_PASSWORD","test-password")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    transport=httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(transport=transport,base_url="http://test") as client:
        denied=await client.get("/dashboard")
        assert denied.status_code==401
        assert "Basic" in denied.headers["www-authenticate"]
        credentials=base64.b64encode(b"viewer:test-password").decode("ascii")
        allowed=await client.get("/dashboard",headers={"Authorization":f"Basic {credentials}"})
        assert allowed.status_code==200
```

## `tests/test_collector.py`

```python
import asyncio
import json
import time
from decimal import Decimal

import pytest

from bromax.collector import MarketCollector
from bromax.config import Settings
from bromax.storage import Store


def _collector(tmp_path):
    settings=Settings(database_path=str(tmp_path/"collector.sqlite3"))
    store=Store(settings.database_path)
    return MarketCollector(settings,store),store


@pytest.mark.asyncio
async def test_live_shaped_tick_runs_existing_strategies_and_persists_forecasts(tmp_path):
    collector,store=_collector(tmp_path)
    start_epoch=1_700_000_000
    prices=[Decimal(f"1234.{index%100:02d}") for index in range(999)]
    times=list(range(start_epoch,start_epoch+len(prices)))
    assert collector._seed_history(
        "1HZ100V",{"prices":prices,"times":times},2
    )==999

    async def no_quotes(*_args):
        return None

    collector._capture_quotes=no_quotes
    tick={
        "symbol":"1HZ100V",
        "epoch":start_epoch+len(prices),
        "quote":Decimal("1234.56"),
        "pip_size":2,
        "id":"subscription-tick-1",
    }
    assert await collector._handle_tick(tick) is True
    await asyncio.sleep(0)

    rows=store.forecasts("1HZ100V")
    state=collector.operational_status("1HZ100V")
    assert len(rows)==len(collector.strategies)
    assert {row["strategy_id"] for row in rows}=={strategy.strategy_id for strategy in collector.strategies}
    assert {row["forecast_epoch"] for row in rows}=={tick["epoch"]}
    assert state["received_ticks"]==state["processed_ticks"]==1
    assert state["analysis_status"]=="ANALYZED"
    assert state["strategy_status"]=="PROCESSED"
    assert state["signal_generation_status"]=="GENERATED"
    assert state["signals_generated"]==len(rows)
    assert store.paper_events()==[]


@pytest.mark.asyncio
async def test_duplicate_invalid_and_out_of_order_ticks_are_counted_and_dropped(tmp_path):
    collector,store=_collector(tmp_path)
    epoch=1_700_000_000
    store.add_tick("1HZ100V",epoch,"1234.50",2,0)
    tick={"symbol":"1HZ100V","epoch":epoch,"quote":Decimal("1234.50"),"pip_size":2}

    assert await collector._handle_tick(tick) is False
    assert await collector._handle_tick({**tick,"epoch":epoch-1}) is False
    with pytest.raises(ValueError,match="missing quote"):
        await collector._handle_tick({"symbol":"1HZ100V","epoch":epoch+1,"pip_size":2})

    state=collector.operational_status("1HZ100V")
    assert state["duplicate_ticks"]==1
    assert state["out_of_order_ticks"]==1
    assert state["invalid_ticks"]==1
    assert len(store.history("1HZ100V"))==1


def test_historical_seed_backfills_missing_epochs_and_reads_chronologically(tmp_path):
    collector,store=_collector(tmp_path)
    prices=[Decimal("1.11"),Decimal("1.12"),Decimal("1.13")]
    assert collector._seed_history(
        "1HZ100V",{"prices":prices,"times":[1700000003,1700000001,1700000002]},2
    )==3
    assert [row["epoch"] for row in store.history("1HZ100V")]==[1700000001,1700000002,1700000003]
    assert collector._seed_history(
        "1HZ100V",
        {"prices":[Decimal("1.10"),Decimal("1.14")],"times":[1700000000,1700000004]},
        2,
    )==2
    assert [row["epoch"] for row in store.history("1HZ100V")]==[
        1700000000,1700000001,1700000002,1700000003,1700000004
    ]


def test_stale_feed_is_distinguished_from_connected_or_receiving_data(tmp_path):
    collector,_store=_collector(tmp_path)
    collector._set_connection_status("CONNECTED")
    state=collector.operational_status("1HZ100V")
    assert state["data_status"]=="NO DATA"
    assert state["pipeline_status"]=="CONNECTED"

    collector.last_received["1HZ100V"]=time.time()-10
    state=collector.operational_status("1HZ100V")
    assert state["data_status"]=="STALE"
    assert state["pipeline_status"]=="STALE DATA"


@pytest.mark.asyncio
async def test_normal_stream_close_reconnects_and_restores_subscription(monkeypatch,tmp_path):
    import bromax.collector as collector_module

    collector,store=_collector(tmp_path)
    second_subscription=asyncio.Event()
    connection_count=0
    subscription_count=0

    class FakeSocket:
        def __init__(self,index):
            self.index=index
            self.history_req_id=None

        async def __aenter__(self):
            return self

        async def __aexit__(self,*_args):
            return None

        async def send(self,raw):
            nonlocal subscription_count
            request=json.loads(raw)
            if "ticks_history" in request:
                self.history_req_id=request["req_id"]
            if request.get("subscribe"):
                subscription_count+=1
                if subscription_count==2:
                    second_subscription.set()

        async def recv(self):
            return json.dumps({
                "req_id":self.history_req_id,
                "history":{"prices":[],"times":[]},
                "pip_size":2,
            })

        def __aiter__(self):
            async def messages():
                if self.index==1:
                    yield json.dumps({
                        "msg_type":"tick",
                        "tick":{"symbol":"1HZ100V","epoch":1700000000,"quote":1234.56,"pip_size":2},
                    })
                else:
                    await asyncio.Event().wait()
            return messages()

    def fake_connect(*_args,**_kwargs):
        nonlocal connection_count
        connection_count+=1
        return FakeSocket(connection_count)

    monkeypatch.setattr(collector_module.websockets,"connect",fake_connect)
    task=asyncio.create_task(collector.run_forever())
    try:
        await asyncio.wait_for(second_subscription.wait(),timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert connection_count==2
    assert subscription_count==2
    assert collector.reconnect_count>=1
    assert collector.connection_status=="DISCONNECTED"
    assert collector.errors==[]
    assert collector.operational_status("1HZ100V")["received_ticks"]==1
    assert len(store.history("1HZ100V"))==1
```

## `tests/test_digits.py`

```python
from decimal import Decimal
import pytest
from bromax.digits import extract_last_digit, quote_precision, QuotePrecisionError

@pytest.mark.parametrize("symbol,quote,pip_size,expected_text,expected_digit",[
    ("1HZ100V","1234.50",2,"1234.50",0),
    ("1HZ100V",Decimal("1234.5"),2,"1234.50",0),
    ("1HZ100V","1234.56",2,"1234.56",6),
    ("1HZ15V","987.1",3,"987.100",0),
    ("R_100","987.123",3,"987.123",3),
    ("R_50","100.00",2,"100.00",0),
    ("R_10","42",2,"42.00",0),
])
def test_last_displayed_digit_by_symbol_precision(symbol,quote,pip_size,expected_text,expected_digit):
    assert extract_last_digit(quote,pip_size)==(expected_text,expected_digit)

def test_zero_decimal_precision_has_no_decimal_digit():
    assert extract_last_digit("1234",0)==("1234",None)

def test_rejects_nonzero_excess_precision():
    with pytest.raises(QuotePrecisionError): extract_last_digit("1.234",2)

def test_rejects_bad_precision():
    with pytest.raises(QuotePrecisionError): quote_precision("2.5")


def test_binary_float_quotes_are_rejected():
    with pytest.raises(QuotePrecisionError): extract_last_digit(1234.5,2)
```

## `tests/test_leakage.py`

```python
import pytest
from bromax.strategies import ForecastContext, candidate_strategies

def test_forecast_context_contains_only_observed_history():
    ctx=ForecastContext("1HZ100V",101,1700000000,(1,2,3,4))
    assert ctx.history==(1,2,3,4)
    assert not hasattr(ctx,"next_tick_digit")
    assert not hasattr(ctx,"actual_next_digit")

class DeliberatePeek:
    strategy_id="deliberate_peek"
    version="test"
    parameters={}
    def forecast(self,context):
        # This must fail: no future outcome is supplied to the forecasting interface.
        return context.next_tick_digit

def test_deliberate_lookahead_attempt_fails():
    ctx=ForecastContext("1HZ100V",101,1700000000,(1,2,3,4))
    with pytest.raises(AttributeError): DeliberatePeek().forecast(ctx)

def test_candidate_forecasts_are_valid_and_use_only_context():
    ctx=ForecastContext("1HZ100V",101,1700000000,tuple([i%10 for i in range(100)]))
    for strategy in candidate_strategies():
        forecast=strategy.forecast(ctx)
        assert 0 <= forecast.prediction <= 9
        assert len(forecast.probabilities)==10
        assert sum(forecast.probabilities)==pytest.approx(1.0)


def test_execution_interface_is_disabled_and_raises():
    import asyncio
    from bromax.paperbot import ExecutionInterface
    interface=ExecutionInterface()
    assert interface.enabled is False
    assert interface.kill_switch_engaged is True
    with pytest.raises(RuntimeError,match="kill switch"):
        asyncio.run(interface.execute())
    interface.engage_kill_switch()
    assert interface.kill_switch_engaged is True
    with pytest.raises(RuntimeError,match="kill switch"):
        asyncio.run(interface.execute())


def test_batch_backtest_matches_online_strategy_definitions():
    import pandas as pd
    from bromax.research import _forecast_frame
    from bromax.strategies import candidate_strategies, ForecastContext
    digits=[(i*7+3)%10 for i in range(1200)]
    ticks=pd.DataFrame({"id":range(1,1201),"epoch":range(1700000000,1700001200),"digit":digits})
    frame=_forecast_frame(ticks,"1HZ100V")
    row=frame[(frame.tick_index==1099)&(frame.strategy_id=="exp_frequency_hl50")].iloc[0]
    ctx=ForecastContext("1HZ100V",1100,1700001099,tuple(digits[100:1100]),tuple(__import__("numpy").bincount(digits[:1100],minlength=10)))
    online=next(s for s in candidate_strategies() if s.strategy_id=="exp_frequency_hl50").forecast(ctx)
    assert int(row.prediction)==online.prediction
    assert list(row.probabilities)==pytest.approx(list(online.probabilities))


def test_cumulative_mode_uses_only_past_count_summary():
    import numpy as np
    from bromax.strategies import CumulativeMode
    counts=tuple(np.bincount([1,1,2,2,2],minlength=10))
    ctx=ForecastContext("1HZ100V",5,1004,(1,1,2,2,2),counts)
    assert CumulativeMode().forecast(ctx).prediction==2


def test_proposal_amount_is_encoded_as_exact_json_decimal():
    import json
    from decimal import Decimal
    from bromax.proposals import encode_proposal_request
    raw=encode_proposal_request("1HZ100V",3,Decimal("1.20"),"USD",1,99)
    assert '"amount": 1.20' in raw
    assert json.loads(raw)["amount"]==1.2


def test_changing_future_digit_cannot_change_forecast_at_prior_tick():
    import pandas as pd
    from bromax.research import _forecast_frame
    digits=[(i*3+7)%10 for i in range(1100)]
    first=pd.DataFrame({"id":range(1,1101),"epoch":range(1700000000,1700001100),"digit":digits})
    changed=digits.copy(); changed[1000]=(changed[1000]+1)%10
    second=first.copy(); second["digit"]=changed
    a=_forecast_frame(first,"1HZ100V")
    b=_forecast_frame(second,"1HZ100V")
    ra=a[a.tick_index==999].sort_values("strategy_id")
    rb=b[b.tick_index==999].sort_values("strategy_id")
    assert ra.prediction.tolist()==rb.prediction.tolist()
    assert list(ra.probabilities)==list(rb.probabilities)


def test_cumulative_mode_batch_prediction_matches_live_context():
    import pandas as pd
    import numpy as np
    from bromax.research import _forecast_frame
    from bromax.strategies import CumulativeMode, ForecastContext
    digits=[(i*11+5)%10 for i in range(1200)]
    ticks=pd.DataFrame({"id":range(1,1201),"epoch":range(1700000000,1700001200),"digit":digits})
    frame=_forecast_frame(ticks,"1HZ100V")
    row=frame[(frame.tick_index==1099)&(frame.strategy_id=="cumulative_mode")].iloc[0]
    ctx=ForecastContext("1HZ100V",1100,1700001099,tuple(digits[100:1100]),tuple(np.bincount(digits[:1100],minlength=10)))
    live=CumulativeMode().forecast(ctx)
    assert int(row.prediction)==live.prediction
    assert list(row.probabilities)==pytest.approx(list(live.probabilities))


def test_proposal_request_rejects_binary_float_stake():
    from bromax.proposals import encode_proposal_request
    with pytest.raises(ValueError): encode_proposal_request("1HZ100V",3,1.2,"USD",1,100)


def test_historical_seed_uses_pip_size_and_never_creates_forecasts(tmp_path):
    from bromax.collector import MarketCollector
    from bromax.config import Settings
    from bromax.storage import Store
    from decimal import Decimal
    store=Store(tmp_path/"seed.sqlite3")
    settings=Settings(database_path=str(tmp_path/"seed.sqlite3"),min_history_ticks=1000)
    collector=MarketCollector(settings,store)
    inserted=collector._seed_history("1HZ100V",{"prices":[Decimal("1234.5"),Decimal("1234.51")],"times":[1700000000,1700000001]},2)
    assert inserted==2
    rows=store.history("1HZ100V",10)
    assert rows[0]["quote_text"]=="1234.50"
    assert rows[0]["digit"]==0
    assert rows[1]["digit"]==1
    assert store.forecasts("1HZ100V")==[]
```

## `tests/test_payout_timing.py`

```python
from bromax.timing import evaluate_payout_timing, _profit_summary


def test_no_quote_settlement_pairs_never_selects_duration():
    report = evaluate_payout_timing([])
    assert report['best_duration'] is None
    assert report['execution_authorized'] is False
    assert report['status'] == 'collecting_quote_backed_settlements'


def test_profit_summary_uses_actual_quoted_stake_and_gross_payout():
    rows = [
        {'stake': '1', 'payout': '9', 'hit': 1, 'duration_ticks': 1},
        {'stake': '1', 'payout': '9', 'hit': 0, 'duration_ticks': 1},
        {'stake': '2', 'payout': '18', 'hit': 1, 'duration_ticks': 1},
    ]
    summary = _profit_summary(rows)
    assert summary['total_stake'] == 4
    assert summary['total_pnl'] == 23  # +8 -1 +16
    assert summary['n'] == 3


def test_quote_settlement_must_be_exact_duration_epoch():
    quote = {'event_type':'QUOTE','forecast_seq':1,'duration_ticks':2,'strategy_id':'s',
             'forecast_epoch':100,'quote_spot_time':'100','stake':'1','payout':'9'}
    wrong = {'event_type':'SETTLE','forecast_seq':1,'duration_ticks':2,'strategy_id':'s',
             'forecast_epoch':100,'actual_epoch':103,'hit':1,'stake':'1','payout':'9'}
    report = evaluate_payout_timing([quote, wrong], min_holdout=1)
    assert report['best_duration'] is None
    assert report['status'] == 'collecting_quote_backed_settlements'


def test_optimizer_selects_only_quote_backed_profitable_holdout_candidate():
    events=[]
    for epoch in range(1,1001):
        seq=epoch
        events.append({'event_type':'QUOTE','forecast_seq':seq,'duration_ticks':1,
                       'strategy_id':'s','strategy_version':'1','forecast_epoch':epoch,
                       'forecast_tick_id':epoch,'prediction':7,'quote_spot_time':str(epoch),
                       'stake':'1','payout':'10','proposal_id':f'p{seq}'})
        hit=int(epoch%4==0)  # 25% wins; 10x gross payout gives positive realized EV.
        events.append({'event_type':'SETTLE','forecast_seq':seq,'duration_ticks':1,
                       'strategy_id':'s','strategy_version':'1','forecast_epoch':epoch,
                       'actual_epoch':epoch+1,'actual_digit':7 if hit else 3,'hit':hit,
                       'stake':'1','payout':'10','paper_pnl':'9' if hit else '-1'})
    report=evaluate_payout_timing(events,min_holdout=100)
    assert report['best_duration']==1
    assert report['best_candidate']['strategy_id']=='s'
    assert report['best_candidate']['final_test']['mean_return_ci95'][0] > 0
    assert report['execution_authorized'] is False
```

## `tests/test_research.py`

```python
import pytest
from bromax.research import wilson, holm_adjust

def test_wilson_interval_contains_observed_rate():
    lo,hi=wilson(10,100)
    assert lo < .10 < hi

def test_holm_adjustment_is_monotone_in_sorted_p_values():
    adjusted=holm_adjust([.01,.04,.03])
    assert adjusted[0] <= adjusted[2] <= adjusted[1]
    assert all(0<=p<=1 for p in adjusted)


def test_validation_finalist_is_the_only_strategy_group_opened_on_final_test(monkeypatch):
    import numpy as np
    import pandas as pd
    from bromax.research import PRE_REGISTERED, _forecast_frame, evaluate_symbol
    monkeypatch.setitem(PRE_REGISTERED["validation_gates"],"minimum_n",100)
    monkeypatch.setitem(PRE_REGISTERED["validation_gates"],"minimum_quoted_n",100)
    rng=np.random.default_rng(7); n=6000
    digits=rng.choice(np.arange(10),n,p=[.30]+[.70/9]*9)
    ticks=pd.DataFrame({"id":range(1,n+1),"epoch":range(1700000000,1700000000+n),"digit":digits})
    frame=_forecast_frame(ticks,"SYNTHETIC_TEST_ONLY")
    quotes=[{"strategy_id":r["strategy_id"],"strategy_version":r["version"],"forecast_tick_id":r["forecast_tick_id"],"prediction":r["prediction"],"probabilities":r["probabilities"],"payout":"8.33","stake":"1.00","actual_digit":r["actual"]} for r in frame.to_dict("records")]
    report=evaluate_symbol(ticks,"SYNTHETIC_TEST_ONLY",quotes)
    digit_evidence=report["digit_evidence"]
    assert len(digit_evidence["digits"])==10
    assert [row["digit"] for row in digit_evidence["digits"]]==list(range(10))
    assert all(0<=row["health_score"]<=100 for row in digit_evidence["digits"])
    assert all(row["decision"]=="DO NOT TRADE" for row in digit_evidence["digits"])
    assert report["validation_finalists"]
    assert report["final_test_accessed"] is True
    assert all(x["final_test"] is None for x in report["strategies"] if x["strategy_id"] not in report["validation_finalists"])
    assert all(x["final_test"] is not None for x in report["strategies"] if x["strategy_id"] in report["validation_finalists"])


def test_verdict_distinguishes_inconclusive_from_no_edge():
    from bromax.research import _verdict
    small={"n":100,"payout_adjusted":{"n_with_actual_quote":100,"quote_coverage":1.0}}
    assert _verdict(small)=="inconclusive"
    enough={"n":6000,"holm_adjusted_p":.9,"hit_rate_block_bootstrap_ci95":[.05,.09],"payout_adjusted":{"n_with_actual_quote":6000,"quote_coverage":1.0,"realized_profit_ci95":[-.2,-.1]}}
    assert _verdict(enough)=="no_evidence_of_edge"


def test_conditional_transition_candidate_is_preregistered_and_causal():
    from bromax.strategies import ConditionalTransition, ForecastContext
    from bromax.research import PRE_REGISTERED, _forecast_frame
    history=tuple(([1,2,3]*60)+[1,2])
    f=ConditionalTransition().forecast(ForecastContext("TEST",8,1700000008,history))
    assert f.strategy_id == "conditional_transition_order2"
    assert f.prediction == 3
    assert f.probabilities[3] > f.probabilities[0]
    assert f.strategy_id in PRE_REGISTERED["candidate_ids"]
    # Batch replay records the same new candidate without reading the outcome early.
    import pandas as pd
    ticks=pd.DataFrame({"id":range(1,1206),"epoch":range(1700000000,1700001205),"digit":[(i%3)+1 for i in range(1205)]})
    frame=_forecast_frame(ticks,"TEST")
    assert "conditional_transition_order2" in set(frame.strategy_id)
    assert frame.tick_index.max() == 1203


def test_intelligence_reports_patterns_without_promoting_them():
    from bromax.intelligence import analyze_ticks
    rows=[{"digit":[1,2,3][i%3],"quote_text":str(100+i/100),"epoch":1700000000+i} for i in range(120)]
    result=analyze_ticks(rows,"TEST",120)
    assert result["patterns"]["conditional_order_2"]["support"] > 0
    assert result["signal_status"] == "RESEARCH_ONLY_NOT_APPROVED"
    assert result["market_state"]["absolute_return_observations"] > 0


def test_conditional_transition_replay_matches_live_window_context():
    import numpy as np
    import pandas as pd
    from bromax.research import _forecast_frame
    from bromax.strategies import ConditionalTransition, ForecastContext
    rng=np.random.default_rng(81)
    digits=rng.integers(0,10,1400).tolist()
    ticks=pd.DataFrame({"id":range(1,1401),"epoch":range(1700000000,1700001400),"digit":digits})
    replay=_forecast_frame(ticks,"TEST")
    target=replay[(replay.strategy_id=="conditional_transition_order2") & (replay.tick_index==1200)].iloc[0]
    h=tuple(digits[1200-999:1201])
    live=ConditionalTransition().forecast(ForecastContext("TEST",1201,1700001200,h))
    assert list(target.probabilities)==pytest.approx(list(live.probabilities))
    assert int(target.prediction)==live.prediction
```

## `tests/test_storage.py`

```python
import pytest
from bromax.storage import Store
from bromax.strategies import Forecast

def test_audit_log_is_append_only(tmp_path):
    store=Store(str(tmp_path/"test.db"))
    from bromax.strategies import Forecast
    tick=store.add_tick("1HZ100V",100,"1.20",2,0,"x")
    f=Forecast("test","1.0",{},0,(1.0,)+(0.0,)*9)
    store.append_forecast("1HZ100V",f,tick,100)
    with store.connect() as c:
        with pytest.raises(Exception): c.execute("UPDATE audit_events SET note='changed' WHERE seq=1")
        with pytest.raises(Exception): c.execute("DELETE FROM ticks")

def test_forecast_scores_only_immediate_next_second(tmp_path):
    store=Store(str(tmp_path/"test.db"))
    tick1=store.add_tick("1HZ100V",100,"1.20",2,0,"a")
    f=Forecast("test","1.0",{},0,(1.0,)+(0.0,)*9)
    store.append_forecast("1HZ100V",f,tick1,100)
    tick_gap=store.add_tick("1HZ100V",102,"1.21",2,1,"b")
    assert store.score_open_forecasts("1HZ100V",tick_gap,102,1)==[]
    tick_next=store.add_tick("1HZ100V",101,"1.20",2,0,"c")
    assert store.score_open_forecasts("1HZ100V",tick_next,101,0)==[(1,1)]

def test_forecast_limit_returns_most_recent_rows_in_chronological_order(tmp_path):
    store=Store(str(tmp_path/"forecast_limit.db"))
    for epoch in range(100,104):
        tick=store.add_tick("1HZ100V",epoch,"1.20",2,0,str(epoch))
        forecast=Forecast("test","1.0",{},epoch%10,(.1,)*10)
        store.append_forecast("1HZ100V",forecast,tick,epoch)
    rows=store.forecasts("1HZ100V",limit=2)
    assert [row["forecast_epoch"] for row in rows]==[102,103]


def test_quote_arriving_after_scoring_is_not_used_as_prediction_time_payout(tmp_path):
    store=Store(str(tmp_path/"late_quote.db"))
    tick=store.add_tick("1HZ100V",100,"1.20",2,0,"x")
    f=Forecast("test","1.0",{},0,(1.0,)+(0.0,)*9)
    seq=store.append_forecast("1HZ100V",f,tick,100)
    # This quote is captured after the forecast's next-epoch deadline (epoch 101).
    store.append_quote("1HZ100V",f,seq,8.33,1.00,"late-proposal")
    assert store.forecasts("1HZ100V")[0]["payout"] is None
    next_tick=store.add_tick("1HZ100V",101,"1.21",2,1,"y")
    store.score_open_forecasts("1HZ100V",next_tick,101,1)
    row=store.forecasts("1HZ100V")[0]
    assert row["actual_digit"]==1
    assert row["payout"] is None
    assert row["stake"] is None


def test_subscription_id_is_not_treated_as_unique_tick_id(tmp_path):
    store=Store(str(tmp_path/"subscription_id.db"))
    first=store.add_tick("1HZ100V",100,"1.20",2,0,"same-subscription-id")
    second=store.add_tick("1HZ100V",101,"1.21",2,1,"same-subscription-id")
    assert first is not None and second is not None and second != first


def test_symbol_histories_are_separate(tmp_path):
    store=Store(str(tmp_path/"symbols.db"))
    store.add_tick("1HZ100V",100,"1.20",2,0,"sub-a")
    store.add_tick("1HZ50V",100,"1.27",2,7,"sub-b")
    assert [r["digit"] for r in store.history("1HZ100V")] == [0]
    assert [r["digit"] for r in store.history("1HZ50V")] == [7]


def test_tick_views_use_market_epoch_not_insert_order(tmp_path):
    store=Store(str(tmp_path/"backfilled_ticks.db"))
    latest_id=store.add_tick("1HZ100V",102,"1.22",2,2,"live")
    store.add_tick("1HZ100V",100,"1.20",2,0,"history-1")
    store.add_tick("1HZ100V",101,"1.21",2,1,"history-2")

    assert [row["epoch"] for row in store.history("1HZ100V")]==[100,101,102]
    assert [row["epoch"] for row in store.quality_history("1HZ100V")]==[100,101,102]
    assert store.ticks_frame("1HZ100V")["epoch"].tolist()==[100,101,102]
    assert store.is_latest_tick("1HZ100V",latest_id)


def test_quote_before_next_epoch_is_retained(tmp_path):
    import time
    store=Store(str(tmp_path/"timely_quote.db"))
    epoch=int(time.time())+1000
    tick=store.add_tick("1HZ100V",epoch,"1.20",2,0,"sub")
    f=Forecast("test","1.0",{},0,(1.0,)+(0.0,)*9)
    seq=store.append_forecast("1HZ100V",f,tick,epoch)
    store.append_quote("1HZ100V",f,seq,8.33,1.00,"timely",epoch)
    nxt=store.add_tick("1HZ100V",epoch+1,"1.21",2,1,"sub")
    store.score_open_forecasts("1HZ100V",nxt,epoch+1,1)
    row=store.forecasts("1HZ100V")[0]
    assert row["actual_digit"]==1
    assert row["payout"]=="8.33"


def test_final_test_snapshot_is_write_once_and_immutable(tmp_path):
    store=Store(str(tmp_path/"snapshot.db"))
    assert store.freeze_research_snapshot("1HZ100V",10000,10000,'{"final_test":"first"}') is True
    assert store.freeze_research_snapshot("1HZ100V",20000,20000,'{"final_test":"second"}') is False
    assert store.research_snapshot("1HZ100V")["report_json"]=='{"final_test":"first"}'
    with store.connect() as c:
        with pytest.raises(Exception): c.execute("UPDATE research_snapshots SET report_json='{}' WHERE symbol='1HZ100V'")
        with pytest.raises(Exception): c.execute("DELETE FROM research_snapshots WHERE symbol='1HZ100V'")


def test_report_uses_frozen_research_snapshot_after_new_data(tmp_path):
    import json
    from bromax.research import generate_report
    store=Store(str(tmp_path/"frozen_report.db"))
    frozen={"symbol":"1HZ100V","n_ticks":10000,"final_test_accessed":True,"validation_finalists":["rolling_mode_100"],"strategies":[]}
    store.freeze_research_snapshot_versioned("1HZ100V","1.1.0",10000,10000,json.dumps(frozen))
    store.add_tick("1HZ100V",9999999999,"1.20",2,0,"subscription")
    report=generate_report(store,["1HZ100V"])
    item=report["symbols"][0]
    assert item["n_ticks"]==10000
    assert item["research_snapshot"]["frozen"] is True


def test_paper_bot_reserves_open_stakes_against_daily_loss_cap(tmp_path):
    from decimal import Decimal
    from bromax.paperbot import PaperBot
    store=Store(str(tmp_path/"risk.db"))
    bot=PaperBot(store,Decimal("1"),Decimal("1"),Decimal("1.50"),10,approvals_path=tmp_path/"none.json")
    assert bot.limits_ok("1HZ100V",Decimal("1")) is True
    tick=store.add_tick("1HZ100V",100,"1.20",2,0,"subscription")
    f=Forecast("test","1.0",{},0,(1.0,)+(0.0,)*9)
    seq=store.append_forecast("1HZ100V",f,tick,100)
    store.append_paper_open("1HZ100V",f,seq,tick,100,Decimal("1"),Decimal("8.33"),"p1",100)
    assert bot.limits_ok("1HZ100V",Decimal("1")) is False
    assert bot.limits_ok("1HZ50V",Decimal("1")) is False  # global daily risk cap spans symbols
    assert bot.approved("1HZ100V","test") is False


def test_paperbot_requires_user_approval_and_machine_passed_frozen_report(tmp_path, monkeypatch):
    import json
    from decimal import Decimal
    from bromax.paperbot import PaperBot
    monkeypatch.chdir(tmp_path)
    (tmp_path/"reports").mkdir()
    approval=tmp_path/"approved_strategies.json"
    approval.write_text(json.dumps({"approved":{"1HZ100V":["rolling_mode_500"]}}))
    test={"n":6000,"holm_adjusted_p":.001,"hit_rate_block_bootstrap_ci95":[.12,.15],"payout_adjusted":{"n_with_actual_quote":6000,"quote_coverage":1.0,"realized_profit_ci95":[.01,.20]}}
    report={"symbols":[{"symbol":"1HZ100V","research_snapshot":{"frozen":True,"protocol_version":"1.1.0"},"validation_finalists":["rolling_mode_500"],"strategies":[{"strategy_id":"rolling_mode_500","final_test":test,"final_test_verdict":"evidence_of_edge"}]}]}
    (tmp_path/"reports"/"bromax_report.json").write_text(json.dumps(report))
    bot=PaperBot(Store(str(tmp_path/"db.sqlite3")),Decimal("1"),Decimal("1"),Decimal("10"),100,approvals_path=approval)
    assert bot.approved("1HZ100V","rolling_mode_500") is True
    assert bot.approved("1HZ100V","cumulative_mode") is False


def test_settings_reject_unsafe_or_inconsistent_paper_limits():
    from decimal import Decimal
    from bromax.config import Settings
    with pytest.raises(ValueError): Settings(paper_stake=Decimal("2"),max_paper_stake=Decimal("1"))
    with pytest.raises(ValueError): Settings(min_history_ticks=100)


def test_proposal_spot_time_must_match_forecast_epoch(tmp_path):
    import time
    store=Store(str(tmp_path/"spot_time.db"))
    epoch=int(time.time())+1000
    tick=store.add_tick("1HZ100V",epoch,"1.20",2,0,"sub")
    f=Forecast("test","1.0",{},0,(1.0,)+(0.0,)*9)
    seq=store.append_forecast("1HZ100V",f,tick,epoch)
    store.append_quote("1HZ100V",f,seq,8.33,1.00,"mismatched",epoch-1)
    nxt=store.add_tick("1HZ100V",epoch+1,"1.21",2,1,"sub")
    store.score_open_forecasts("1HZ100V",nxt,epoch+1,1)
    assert store.forecasts("1HZ100V")[0]["payout"] is None


def test_settings_reject_zero_pending_quote_queue():
    from bromax.config import Settings
    with pytest.raises(ValueError): Settings(max_pending_quote_tasks=0)


def test_paper_bot_rejects_current_quote_with_nonpositive_model_ev(tmp_path, monkeypatch):
    from decimal import Decimal
    from bromax.paperbot import PaperBot
    from bromax.strategies import Forecast
    store=Store(str(tmp_path/"ev_gate.db"))
    bot=PaperBot(store,Decimal("1"),Decimal("1"),Decimal("10"),100,approvals_path=tmp_path/"none.json")
    bot.approved=lambda symbol,strategy_id: True
    f=Forecast("test","1.0",{},0,(.10,)+(.10,)*9)
    assert bot.record("1HZ100V",f,1,1,100,Decimal("1"),Decimal("8"),"p",100)=="current_quote_model_ev_nonpositive"


def test_versioned_research_snapshots_preserve_prior_protocols(tmp_path):
    store=Store(tmp_path/"versioned.sqlite3")
    assert store.freeze_research_snapshot_versioned("1HZ100V","1.1.0",12000,12000,'{"protocol":"1.1.0"}') is True
    assert store.freeze_research_snapshot_versioned("1HZ100V","1.1.0",13000,13000,'{"protocol":"changed"}') is False
    assert store.research_snapshot_versioned("1HZ100V","1.1.0")["report_json"]=='{"protocol":"1.1.0"}'
    assert store.research_snapshot_versioned("1HZ100V","1.2.0") is None
    with store.connect() as c:
        with pytest.raises(Exception):
            c.execute("UPDATE research_snapshots_v2 SET report_json='{}' WHERE symbol='1HZ100V'")


def test_settings_protocol_requires_one_second_symbols():
    from bromax.config import Settings
    with pytest.raises(ValueError, match="1HZ"):
        Settings(symbols=("R_100",))


def test_duration_quote_ledger_captures_actual_quote_and_exact_settlement(tmp_path):
    import time
    from decimal import Decimal
    store=Store(str(tmp_path/"duration.sqlite3"))
    epoch=int(time.time())+1000
    tick=store.add_tick("1HZ100V",epoch,"1.20",2,0,"x")
    f=Forecast("test","1.0",{},2,(0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1))
    seq=store.append_forecast("1HZ100V",f,tick,epoch)
    assert store.append_duration_quote("1HZ100V",f,seq,tick,epoch,2,Decimal("1"),Decimal("9"),"proposal",epoch)
    target=store.add_tick("1HZ100V",epoch+2,"1.23",2,3,"y")
    settled=store.settle_duration_contracts("1HZ100V",target,epoch+2,3)
    assert settled[0]["duration_ticks"] == 2
    assert settled[0]["hit"] == 0
    assert settled[0]["pnl"] == "-1"
    assert len(store.duration_events("1HZ100V")) == 2
    with store.connect() as c:
        with pytest.raises(Exception): c.execute("UPDATE duration_events SET payout='100' WHERE event_type='QUOTE'")
```

## `tests/test_timing.py`

```python
from bromax.timing import evaluate_timing, wilson


def test_wilson_bounds():
    lo, hi = wilson(10, 100)
    assert 0 < lo < .1 < hi < 1


def test_timing_uses_future_horizon_and_never_authorizes_execution():
    ticks = [
        {'id': i+1, 'epoch': 1000+i, 'digit': i % 10, 'quote_text': f'{100+i/100:.2f}', 'symbol': '1HZ100V'}
        for i in range(80)
    ]
    forecasts = [{'event_type':'FORECAST','forecast_tick_id':i+1,'prediction':(i+1)%10,'strategy_id':'test'} for i in range(70)]
    result = evaluate_timing(ticks, forecasts, durations=(1,2,3))
    assert result['execution_authorized'] is False
    assert result['durations_tested_ticks'] == [1,2,3]
    assert {x['duration_ticks'] for x in result['strategies']} == {1,2,3}
    assert result['best_duration'] is None


def test_missing_epoch_gap_is_not_scored():
    ticks = [
        {'id':1,'epoch':100,'digit':1,'quote_text':'1.1'},
        {'id':2,'epoch':101,'digit':2,'quote_text':'1.2'},
        {'id':3,'epoch':103,'digit':3,'quote_text':'1.3'},
        {'id':4,'epoch':104,'digit':4,'quote_text':'1.4'},
    ]
    forecasts = [{'forecast_tick_id':1,'prediction':2,'strategy_id':'s'}]
    result = evaluate_timing(ticks, forecasts, durations=(1,2))
    rows = {x['duration_ticks']:x for x in result['strategies']}
    assert rows[1]['n'] == 1
    assert 2 not in rows


def test_volatility_gate_is_fail_closed_without_holdout_evidence():
    ticks = [
        {'id': i+1, 'epoch': 2000+i, 'digit': (i*7)%10,
         'quote_text': f'{100+i/100:.2f}', 'symbol': '1HZ100V'}
        for i in range(120)
    ]
    forecasts = [
        {'forecast_tick_id': i+1, 'prediction': (i*3)%10, 'strategy_id': 'candidate'}
        for i in range(100)
    ]
    result = evaluate_timing(ticks, forecasts, durations=(1,))
    assert result['execution_authorized'] is False
    row = result['strategies'][0]
    assert all(x['status'] == 'UNCONFIRMED' for x in row['volatility_regimes'])
```
