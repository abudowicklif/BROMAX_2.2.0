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
from .strategies import candidate_strategies

AgentStatus = Literal["APPROVED", "REJECTED", "INSUFFICIENT_EVIDENCE", "MONITORING", "ERROR"]
log = logging.getLogger("bromax.agents")

AGENT_ROLES = {
    "data_intelligence": "live data quality gate",
    "strategy_research": "strategy validation gate",
    "probability_calibration": "forecast integrity and disagreement check",
    "market_regime": "current volatility regime gate",
    "contract_intelligence": "paper contract quote and expected-value check",
    "risk_signal_approval": "paper-only risk veto",
    "performance_learning": "historical outcome monitoring",
    "chief_intelligence_coordinator": "paper decision consolidation",
}
RESEARCH_SELECTION_GATES = (
    "data_intelligence",
    "strategy_research",
    "probability_calibration",
    "market_regime",
)


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
    except Exception as error:
        log.exception("Agent %s failed; continuing the coordinated analysis", agent_id)
        result = _result(
            agent_id,
            "ERROR",
            "Agent execution failed; see the recorded error.",
            (f"{type(error).__name__}: {error}",),
            {"error_type": type(error).__name__},
        )
    result = replace(result, duration_ms=round((time.perf_counter() - started) * 1000, 3))
    log.info("Agent %s status=%s duration_ms=%.3f", agent_id, result.status, result.duration_ms)
    return result


def _strategy_batch_summary(signal_data: dict[str, Any]) -> dict[str, Any]:
    expected_ids = {strategy.strategy_id for strategy in candidate_strategies()}
    rows = signal_data.get("signals") or []
    strategy_ids = [
        str(row.get("strategy_id"))
        for row in rows
        if isinstance(row, dict) and row.get("strategy_id")
    ]
    received_ids = set(strategy_ids)
    duplicates = sorted({strategy_id for strategy_id in strategy_ids if strategy_ids.count(strategy_id) > 1})
    try:
        tick_id = int(signal_data["observed_tick_id"])
        epoch = int(signal_data["observed_epoch"])
    except (KeyError, TypeError, ValueError):
        tick_id = epoch = None
    valid = (
        signal_data.get("status") not in {"analysis_pending", "insufficient_history"}
        and bool(signal_data.get("symbol"))
        and tick_id is not None
        and epoch is not None
        and len(strategy_ids) == len(rows) == len(expected_ids)
        and received_ids == expected_ids
        and not duplicates
    )
    return {
        "status": "COMPLETE" if valid else "INCOMPLETE",
        "symbol": signal_data.get("symbol"),
        "observed_tick_id": tick_id,
        "observed_epoch": epoch,
        "expected_count": len(expected_ids),
        "received_count": len(rows),
        "missing_strategy_ids": sorted(expected_ids - received_ids),
        "unexpected_strategy_ids": sorted(received_ids - expected_ids),
        "duplicate_strategy_ids": duplicates,
        "invalid_rows": len(rows) - len(strategy_ids),
    }


def build_combined_candidate(
    signal_data: dict[str, Any],
    *,
    feed_fresh: bool,
    snapshot_consistent: bool = True,
    agent_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine the registered strategy distributions without qualifying a signal."""
    batch = _strategy_batch_summary(signal_data)
    epoch = batch["observed_epoch"]
    candidate = {
        "candidate_id": None,
        "status": "WAITING_FOR_HISTORY",
        "symbol": signal_data.get("symbol"),
        "digit": None,
        "model_probability": None,
        "probabilities": None,
        "aggregation_method": "equal_weight_probability_mean",
        "contributing_strategy_count": 0,
        "expected_strategy_count": batch["expected_count"],
        "strategy_ids": [],
        "observed_tick_id": batch["observed_tick_id"],
        "observed_epoch": epoch,
        "target_epoch": epoch + 1 if epoch is not None else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "validation_status": "PENDING",
        "correlation_status": "PENDING",
        "probability_is_calibrated": False,
        "trade_authorized": False,
        "execution_authorized": False,
        "reason": "Waiting for a fresh complete strategy batch.",
    }
    if agent_evidence is not None:
        candidate["agent_evidence"] = agent_evidence

    snapshot_status = signal_data.get("status")
    if snapshot_status == "insufficient_history":
        candidate["status"] = "WAITING_FOR_HISTORY"
        return candidate
    if snapshot_status == "analysis_pending":
        candidate["status"] = "ANALYSIS_PENDING"
        candidate["reason"] = "The current tick does not yet have a complete strategy batch."
        return candidate
    if not feed_fresh:
        candidate["status"] = "STALE_MARKET_DATA"
        candidate["reason"] = "A combined candidate is not current while the live feed is stale."
        return candidate
    if not snapshot_consistent:
        candidate["status"] = "INCONSISTENT_SNAPSHOT"
        candidate["reason"] = "The market tick changed while the candidate snapshot was assembled."
        return candidate
    if batch["status"] != "COMPLETE":
        candidate["status"] = "INCOMPLETE_STRATEGY_BATCH"
        candidate["reason"] = "Every registered strategy must contribute to the same tick."
        return candidate

    rows_by_id = {
        str(row["strategy_id"]): row
        for row in signal_data["signals"]
        if isinstance(row, dict) and row.get("strategy_id")
    }
    strategy_ids = [strategy.strategy_id for strategy in candidate_strategies()]
    vectors = []
    strategy_predictions = []
    invalid_strategy_ids = []
    for strategy_id in strategy_ids:
        row = rows_by_id[strategy_id]
        raw_probabilities = row.get("probabilities")
        try:
            if not isinstance(raw_probabilities, (list, tuple)) or len(raw_probabilities) != 10:
                raise ValueError("expected ten probabilities")
            if any(isinstance(value, bool) for value in raw_probabilities):
                raise ValueError("boolean probability")
            probabilities = [float(value) for value in raw_probabilities]
        except (TypeError, ValueError):
            invalid_strategy_ids.append(strategy_id)
            continue
        if (
            any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities)
            or not math.isclose(math.fsum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-6)
        ):
            invalid_strategy_ids.append(strategy_id)
            continue
        vectors.append(probabilities)
        strategy_predictions.append({
            "strategy_id": strategy_id,
            "predicted_digit": row.get("predicted_digit"),
        })

    if invalid_strategy_ids or len(vectors) != len(strategy_ids):
        candidate["status"] = "INVALID_STRATEGY_OUTPUT"
        candidate["invalid_strategy_ids"] = invalid_strategy_ids or strategy_ids
        candidate["reason"] = "All strategy probability vectors must be finite ten-digit distributions."
        return candidate

    combined = [
        math.fsum(vector[digit] for vector in vectors) / len(vectors)
        for digit in range(10)
    ]
    total_probability = math.fsum(combined)
    if not math.isfinite(total_probability) or total_probability <= 0:
        candidate["status"] = "INVALID_STRATEGY_OUTPUT"
        candidate["reason"] = "The combined probability vector is invalid."
        return candidate
    combined = [probability / total_probability for probability in combined]
    digit = max(range(10), key=combined.__getitem__)
    candidate.update({
        "candidate_id": f"{batch['symbol']}:{batch['observed_tick_id']}:equal_weight_mean",
        "status": "UNVALIDATED_CANDIDATE",
        "digit": digit,
        "model_probability": combined[digit],
        "probabilities": combined,
        "contributing_strategy_count": len(strategy_ids),
        "strategy_ids": strategy_ids,
        "strategy_predictions": strategy_predictions,
        "reason": "Combined research candidate only; ensemble validation and correlation review are pending.",
    })
    return candidate


def _coordination_summary(
    signal_data: dict[str, Any],
    results: list[AgentResult],
    consistent: bool,
) -> dict[str, Any]:
    strategy_batch = _strategy_batch_summary(signal_data)
    by_id = {result.agent_id: result for result in results}
    missing_agents = [agent_id for agent_id in AGENT_ROLES if agent_id not in by_id]
    gate_statuses = {
        agent_id: by_id[agent_id].status
        for agent_id in RESEARCH_SELECTION_GATES
        if agent_id in by_id
    }

    if not consistent:
        status = "INCONSISTENT_SNAPSHOT"
    elif strategy_batch["status"] != "COMPLETE":
        status = "INCOMPLETE_STRATEGY_BATCH"
    elif missing_agents:
        status = "MISSING_REQUIRED_AGENT"
    elif any(result.status == "ERROR" for result in results):
        status = "AGENT_ERROR"
    elif any(value == "REJECTED" for value in gate_statuses.values()):
        status = "REJECTED"
    elif any(value != "APPROVED" for value in gate_statuses.values()):
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "READY_FOR_SELECTION"

    contributions = []
    for result in results:
        contribution_status = {
            "APPROVED": "ACCEPTED",
            "REJECTED": "REJECTED",
            "INSUFFICIENT_EVIDENCE": "UNAVAILABLE",
            "MONITORING": "MONITORING",
            "ERROR": "ERROR",
        }[result.status]
        contributions.append({
            "agent_id": result.agent_id,
            "role": AGENT_ROLES.get(result.agent_id, "unregistered agent"),
            "status": result.status,
            "contribution_status": contribution_status,
            "summary": result.summary,
            "duration_ms": result.duration_ms,
            "reasons": list(result.reasons),
        })

    return {
        "status": status,
        "selection_ready": status == "READY_FOR_SELECTION",
        "snapshot_consistent": consistent,
        "snapshot_tick_id": strategy_batch["observed_tick_id"],
        "snapshot_epoch": strategy_batch["observed_epoch"],
        "strategy_batch": strategy_batch,
        "required_research_gates": gate_statuses,
        "missing_required_agents": missing_agents,
        "agent_contributions": contributions,
        "paper_signal_status": by_id.get("risk_signal_approval").status
        if by_id.get("risk_signal_approval") else "UNAVAILABLE",
        "selection": "DEFERRED_TO_QUALIFICATION_STAGE",
    }


def select_primary_signal(
    signal_data: dict[str, Any],
    agent_results: list[dict[str, Any]],
    coordination: dict[str, Any],
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    symbol = signal_data.get("symbol")
    tick_id = coordination.get("snapshot_tick_id")
    epoch = coordination.get("snapshot_epoch")
    reasons = []
    by_id = {result.get("agent_id"): result for result in agent_results}

    strategy_batch = coordination.get("strategy_batch")
    if strategy_batch is None:
        strategy_batch = _strategy_batch_summary(signal_data)
    if strategy_batch.get("status") != "COMPLETE":
        reasons.append("The current tick does not have a complete registered strategy batch.")
    if (
        tick_id != signal_data.get("observed_tick_id")
        or epoch != signal_data.get("observed_epoch")
    ):
        reasons.append("The strategy batch does not match the coordinated tick.")
    if not coordination.get("selection_ready"):
        reasons.append(f"Required analysis is not ready ({coordination.get('status', 'UNKNOWN')}).")
        for agent_id in RESEARCH_SELECTION_GATES:
            result = by_id.get(agent_id)
            if result is None:
                reasons.append(f"Required analysis is unavailable: {agent_id}.")
            elif result.get("status") != "APPROVED":
                detail = result.get("summary") or result.get("status") or "not approved"
                reasons.append(f"{agent_id}: {detail}")

    strategy_result = by_id.get("strategy_research") or {}
    strategy_id = (strategy_result.get("metrics") or {}).get("selected_strategy_id")
    strategy_rows = [
        row for row in (signal_data.get("signals") or [])
        if isinstance(row, dict)
    ]
    selected_row = next(
        (row for row in strategy_rows if row.get("strategy_id") == strategy_id),
        None,
    ) if strategy_id else None
    probability_result = by_id.get("probability_calibration") or {}
    candidate_digits = (probability_result.get("output") or {}).get("candidate_digits") or []

    if not strategy_id:
        reasons.append("Research validation did not identify a selected strategy.")
    if selected_row is None:
        reasons.append("The research-selected strategy has no forecast for the current tick.")
    if selected_row is not None:
        try:
            digit = int(selected_row["predicted_digit"])
            probabilities = [float(value) for value in selected_row["probabilities"]]
            model_probability = probabilities[digit] if digit in range(10) and len(probabilities) == 10 else None
            published_probability = float(selected_row["predicted_digit_probability"])
        except (KeyError, TypeError, ValueError, IndexError):
            digit = None
            probabilities = []
            model_probability = None
            published_probability = None
            reasons.append("The selected strategy forecast is incomplete.")
        if digit not in range(10):
            reasons.append("The selected strategy forecast contains an invalid digit.")
        if (
            len(probabilities) != 10
            or any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities)
            or not math.isclose(sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-6)
        ):
            reasons.append("The selected strategy probability vector is invalid.")
        if (
            model_probability is not None
            and published_probability is not None
            and not math.isclose(model_probability, published_probability, rel_tol=0.0, abs_tol=1e-12)
        ):
            reasons.append("The selected strategy's displayed probability does not match its vector.")
        if digit not in candidate_digits:
            reasons.append("The selected digit is not present in the validated strategy outputs.")

    if reasons:
        return {
            "status": "NO QUALIFIED SIGNAL",
            "qualification_status": "BLOCKED",
            "symbol": symbol,
            "digit": None,
            "strategy_id": None,
            "observed_tick_id": tick_id,
            "observed_epoch": epoch,
            "target_epoch": int(epoch) + 1 if epoch is not None else None,
            "timestamp": evaluated_at or datetime.now(timezone.utc).isoformat(),
            "model_probability": None,
            "probability_is_calibrated": False,
            "strategy_agreement": None,
            "reasons": list(dict.fromkeys(reasons)),
            "trade_authorized": False,
        }

    agreement_count = sum(
        int(row.get("predicted_digit", -1)) == digit
        for row in strategy_rows
    )
    research_metrics = strategy_result.get("metrics") or {}
    regime_metrics = (by_id.get("market_regime") or {}).get("metrics") or {}
    data_metrics = (by_id.get("data_intelligence") or {}).get("metrics") or {}
    calibration_metrics = probability_result.get("metrics") or {}
    return {
        "status": "QUALIFIED",
        "qualification_status": "QUALIFIED_RESEARCH_SIGNAL",
        "symbol": symbol,
        "digit": digit,
        "strategy_id": strategy_id,
        "observed_tick_id": tick_id,
        "observed_epoch": epoch,
        "target_epoch": int(epoch) + 1 if epoch is not None else None,
        "timestamp": evaluated_at or datetime.now(timezone.utc).isoformat(),
        "model_probability": model_probability,
        "probability_is_calibrated": False,
        "evidence": {
            "validation_finalist": research_metrics.get("validation_finalist"),
            "final_test_verdict": research_metrics.get("final_test_verdict"),
            "final_test_n": research_metrics.get("final_test_n"),
            "market_regime": regime_metrics.get("current_regime"),
            "data_quality_score": data_metrics.get("quality_score"),
            "mean_validation_calibration_error": calibration_metrics.get("mean_validation_calibration_error"),
        },
        "strategy_agreement": {
            "agreeing_strategies": agreement_count,
            "total_strategies": len(strategy_rows),
            "used_as_weight": False,
        },
        "reasons": [],
        "trade_authorized": False,
    }


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
            estimated_probabilities = []
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
                        estimated_probabilities.append(probability)
                health = row.get("health_percent")
                if health is not None:
                    health = float(health)
                    if not math.isfinite(health) or not 0 <= health <= 100:
                        reasons.append(f"Digit {row['digit']} has an invalid evidence-health score.")
                    else:
                        health_scores.append(health)
                calibration = (row.get("validation") or {}).get("absolute_calibration_error")
                if calibration is not None:
                    calibration = float(calibration)
                    if not math.isfinite(calibration) or not 0 <= calibration <= 1:
                        reasons.append(f"Digit {row['digit']} has an invalid calibration error.")
                    else:
                        calibration_errors.append(calibration)
            vectors = signals.get("signals") or []
            disagreement = set()
            if vectors:
                strategy_ids = [str(signal.get("strategy_id") or "") for signal in vectors]
                expected_strategy_ids = {
                    strategy.strategy_id for strategy in candidate_strategies()
                }
                if (
                    len(strategy_ids) != len(set(strategy_ids))
                    or set(strategy_ids) != expected_strategy_ids
                ):
                    reasons.append(
                        "Current forecasts must contain exactly one output from every registered strategy."
                    )
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
            if not math.isclose(sum(estimated_probabilities), 1.0, rel_tol=0.0, abs_tol=1e-6):
                return _result(
                    self.agent_id, "REJECTED", "Per-digit model probabilities do not form a distribution.",
                    ("The ten estimated digit probabilities must sum to one.",),
                    {"digit_rows": len(rows), "valid_digit_estimates": estimates},
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
            exploratory = timing.get("exploratory_diagnostics") or {}
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
                    "exploratory_sample_size": exploratory.get("sample_size"),
                    "digit_counts": exploratory.get("digit_counts"),
                    "uniformity_diagnostic": exploratory.get("uniformity_diagnostic"),
                    "conditional_patterns": exploratory.get("patterns"),
                    "transition_matrix_smoothed": exploratory.get("transition_matrix_smoothed"),
                },
            )
        return _run(self.agent_id, evaluate)


class ContractIntelligenceAgent:
    """Input: approved-ready candidate and its digit/duration quote. Output: EV verification."""

    agent_id = "contract_intelligence"

    def run(self, ready: dict[str, Any], digit_analysis: dict[str, Any]):
        def evaluate():
            is_ready = ready.get("eligible") is True
            signal = ready.get("signal") if is_ready else ready.get("research_candidate")
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
            if not is_ready:
                reasons.append(
                    ready.get("reason") or "Exploratory quote review is not an approved paper signal."
                )
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
    coordination=_coordination_summary(signal_data,results,consistent)
    tick_id=signal_data.get("observed_tick_id")
    coordination["handoffs"]=[
        {
            "from_agent":result.agent_id,
            "to_agent":"risk_signal_approval",
            "source_tick_id":tick_id,
            "result_status":result.status,
            "summary":result.summary,
            "transport":"in_process_function_input",
        }
        for result in first_five
    ]+[
        {
            "from_agent":result.agent_id,
            "to_agent":"chief_intelligence_coordinator",
            "source_tick_id":tick_id,
            "result_status":result.status,
            "summary":result.summary,
            "transport":"in_process_function_input",
        }
        for result in specialist_results
    ]
    agent_evidence = {
        "status": coordination["status"],
        "snapshot_tick_id": tick_id,
        "matches_candidate_tick": bool(
            consistent and tick_id == signal_data.get("observed_tick_id")
        ),
        "complete_agent_count": sum(result.status != "ERROR" for result in results),
        "required_agent_count": len(AGENT_ROLES),
        "agent_statuses": {result.agent_id: result.status for result in results},
    }
    combined_candidate = build_combined_candidate(
        signal_data,
        feed_fresh=not feed_stale,
        snapshot_consistent=consistent,
        agent_evidence=agent_evidence,
    )
    primary_signal=select_primary_signal(
        signal_data, [result.as_dict() for result in results], coordination, timestamp
    )
    coordination["selection"]=primary_signal["status"]
    return {
        "decision":decision,
        "agents":[result.as_dict() for result in results],
        "performance":performance_result.metrics,
        "coordination":coordination,
        "combined_candidate":combined_candidate,
        "primary_signal":primary_signal,
    }
