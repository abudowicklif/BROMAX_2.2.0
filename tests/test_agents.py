from datetime import datetime, timedelta, timezone

from bromax.agents import (
    AgentResult,
    ChiefIntelligenceCoordinator,
    ContractIntelligenceAgent,
    DataIntelligenceAgent,
    PerformanceLearningAgent,
    ProbabilityCalibrationAgent,
    _strategy_batch_summary,
    build_combined_candidate,
    coordinate_snapshot,
    select_primary_signal,
)
from bromax.storage import Store
from bromax.strategies import candidate_strategies


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


def test_probability_agent_rejects_incomplete_registered_strategy_batch():
    analysis = {
        "digits": [
            {"digit": digit, "estimated_win_probability": 0.1, "health_percent": None}
            for digit in range(10)
        ]
    }
    strategy = candidate_strategies()[0]
    result = ProbabilityCalibrationAgent().run(analysis, {
        "signals": [{
            "strategy_id": strategy.strategy_id,
            "predicted_digit": 0,
            "probabilities": [0.1] * 10,
        }]
    })
    assert result.status == "REJECTED"
    assert any("every registered strategy" in reason for reason in result.reasons)


def test_probability_agent_rejects_estimates_that_do_not_sum_to_one():
    analysis = {
        "digits": [
            {"digit": digit, "estimated_win_probability": 0.2, "health_percent": None}
            for digit in range(10)
        ]
    }
    signals = {
        "signals": [
            {
                "strategy_id": strategy.strategy_id,
                "predicted_digit": 0,
                "probabilities": [0.1] * 10,
            }
            for strategy in candidate_strategies()
        ]
    }
    result = ProbabilityCalibrationAgent().run(analysis, signals)
    assert result.status == "REJECTED"
    assert any("must sum to one" in reason for reason in result.reasons)


def test_strategy_batch_requires_all_registered_strategies_on_one_tick():
    strategy_ids = [strategy.strategy_id for strategy in candidate_strategies()]
    signal_data = {
        "symbol": "1HZ100V",
        "observed_tick_id": 42,
        "observed_epoch": 1700000042,
        "signals": [{"strategy_id": strategy_id} for strategy_id in strategy_ids],
    }
    complete = _strategy_batch_summary(signal_data)
    assert complete["status"] == "COMPLETE"
    assert complete["received_count"] == 10
    assert complete["missing_strategy_ids"] == []

    partial = _strategy_batch_summary({**signal_data, "signals": signal_data["signals"][:-1]})
    assert partial["status"] == "INCOMPLETE"
    assert len(partial["missing_strategy_ids"]) == 1


def test_combined_candidate_equal_weights_all_strategy_vectors_without_authorization():
    strategies = candidate_strategies()
    signal_data = {
        "symbol": "1HZ100V",
        "status": "ready",
        "observed_tick_id": 42,
        "observed_epoch": 1700000042,
        "signals": [],
    }
    for index, strategy in enumerate(strategies):
        digit = 3 if index < len(strategies) // 2 else 7
        probabilities = [0.0] * 10
        probabilities[digit] = 1.0
        signal_data["signals"].append({
            "strategy_id": strategy.strategy_id,
            "predicted_digit": digit,
            "probabilities": probabilities,
        })

    candidate = build_combined_candidate(
        signal_data,
        feed_fresh=True,
        agent_evidence={
            "status": "PENDING",
            "matches_candidate_tick": False,
            "complete_agent_count": 0,
            "required_agent_count": 8,
        },
    )

    assert candidate["status"] == "UNVALIDATED_CANDIDATE"
    assert candidate["digit"] == 3
    assert candidate["model_probability"] == 0.5
    assert candidate["probabilities"][7] == 0.5
    assert candidate["contributing_strategy_count"] == 10
    assert candidate["target_epoch"] == signal_data["observed_epoch"] + 1
    assert candidate["probability_is_calibrated"] is False
    assert candidate["validation_status"] == "PENDING"
    assert candidate["correlation_status"] == "PENDING"
    assert candidate["agent_evidence"]["matches_candidate_tick"] is False
    assert candidate["trade_authorized"] is False
    assert candidate["execution_authorized"] is False


def test_combined_candidate_hides_digit_for_stale_inconsistent_or_incomplete_data():
    strategies = candidate_strategies()
    signal_data = {
        "symbol": "1HZ100V",
        "status": "ready",
        "observed_tick_id": 42,
        "observed_epoch": 1700000042,
        "signals": [{
            "strategy_id": strategy.strategy_id,
            "predicted_digit": 2,
            "probabilities": [0.1] * 10,
        } for strategy in strategies],
    }

    stale = build_combined_candidate(signal_data, feed_fresh=False)
    inconsistent = build_combined_candidate(
        signal_data, feed_fresh=True, snapshot_consistent=False
    )
    incomplete = build_combined_candidate(
        {**signal_data, "signals": signal_data["signals"][:-1]},
        feed_fresh=True,
    )
    invalid = build_combined_candidate(
        {
            **signal_data,
            "signals": [
                {**row, "probabilities": [0.2] * 10}
                for row in signal_data["signals"]
            ],
        },
        feed_fresh=True,
    )

    assert stale["status"] == "STALE_MARKET_DATA"
    assert inconsistent["status"] == "INCONSISTENT_SNAPSHOT"
    assert incomplete["status"] == "INCOMPLETE_STRATEGY_BATCH"
    assert invalid["status"] == "INVALID_STRATEGY_OUTPUT"
    assert all(
        candidate["digit"] is None
        for candidate in (stale, inconsistent, incomplete, invalid)
    )


def test_coordination_separates_research_readiness_from_paper_quote_gates():
    strategies = candidate_strategies()
    selected_id = strategies[0].strategy_id
    signal_data = {
        "symbol": "1HZ100V",
        "observed_tick_id": 3,
        "observed_epoch": 102,
        "signals": [
            {
                "strategy_id": strategy.strategy_id,
                "predicted_digit": 0,
                "predicted_digit_probability": 0.1,
                "probabilities": [0.1] * 10,
            }
            for strategy in strategies
        ],
    }
    report_item = {
        "digit_evidence": {"selected_strategy_id": selected_id},
        "validation_finalists": [selected_id],
        "strategies": [{
            "strategy_id": selected_id,
            "validation": {"n": 6000, "payout_adjusted": {"quote_coverage": 1.0}},
            "final_test": {"n": 6000},
            "final_test_verdict": "evidence_of_edge",
        }],
    }
    digit_analysis = {
        "digits": [
            {"digit": digit, "estimated_win_probability": 0.1, "health_percent": None}
            for digit in range(10)
        ],
        "volatility_filter": {"status": "PASS", "regime": "LOW"},
    }
    coordinated = coordinate_snapshot(
        report_item=report_item,
        digit_analysis=digit_analysis,
        signal_data=signal_data,
        timing={"strategies": []},
        ready={"status": "WAITING", "eligible": False, "signal": None},
        quality_ticks=_ticks(),
        feed_stale=False,
        max_data_age_seconds=5,
        collector_errors=[],
        paper_events=[],
        forecasts=[],
        prior_runs=[],
        consistent=True,
    )

    assert coordinated["coordination"]["strategy_batch"]["status"] == "COMPLETE"
    assert coordinated["coordination"]["status"] == "READY_FOR_SELECTION"
    assert coordinated["coordination"]["selection_ready"] is True
    assert coordinated["coordination"]["paper_signal_status"] == "INSUFFICIENT_EVIDENCE"
    assert coordinated["coordination"]["selection"] == "QUALIFIED"
    assert len(coordinated["coordination"]["agent_contributions"]) == 8
    assert coordinated["primary_signal"]["digit"] == 0
    assert coordinated["primary_signal"]["probability_is_calibrated"] is False
    assert coordinated["primary_signal"]["trade_authorized"] is False
    assert coordinated["combined_candidate"]["status"] == "UNVALIDATED_CANDIDATE"
    assert coordinated["combined_candidate"]["digit"] == 0
    assert coordinated["combined_candidate"]["trade_authorized"] is False
    assert coordinated["decision"]["trade_authorized"] is False


def test_primary_signal_uses_research_selected_strategy_not_majority_vote():
    strategies = candidate_strategies()
    selected_id = strategies[1].strategy_id
    signal_rows = []
    for strategy in strategies:
        if strategy.strategy_id == selected_id:
            digit = 2
            probabilities = [0.02, 0.02, 0.82] + [0.02] * 7
        elif strategy.strategy_id == "uniform_random":
            digit = 9
            probabilities = [0.1] * 10
        else:
            digit = 9
            probabilities = [0.02] * 9 + [0.82]
        signal_rows.append({
            "strategy_id": strategy.strategy_id,
            "predicted_digit": digit,
            "predicted_digit_probability": probabilities[digit],
            "probabilities": probabilities,
        })
    signal_data = {
        "symbol": "1HZ100V",
        "observed_tick_id": 88,
        "observed_epoch": 1700000088,
        "signals": signal_rows,
    }
    agent_results = [
        {
            "agent_id": "strategy_research",
            "status": "APPROVED",
            "metrics": {
                "selected_strategy_id": selected_id,
                "validation_finalist": True,
                "final_test_verdict": "evidence_of_edge",
                "final_test_n": 6000,
            },
        },
        {
            "agent_id": "probability_calibration",
            "status": "APPROVED",
            "metrics": {"mean_validation_calibration_error": 0.05},
            "output": {"candidate_digits": [2, 9]},
        },
        {
            "agent_id": "market_regime",
            "status": "APPROVED",
            "metrics": {"current_regime": "LOW"},
        },
        {
            "agent_id": "data_intelligence",
            "status": "APPROVED",
            "metrics": {"quality_score": 100},
        },
    ]
    coordination = {
        "status": "READY_FOR_SELECTION",
        "selection_ready": True,
        "snapshot_tick_id": 88,
        "snapshot_epoch": 1700000088,
        "strategy_batch": {"status": "COMPLETE"},
    }

    result = select_primary_signal(signal_data, agent_results, coordination)

    assert result["status"] == "QUALIFIED"
    assert result["digit"] == 2
    assert result["strategy_id"] == selected_id
    assert result["model_probability"] == 0.82
    assert result["strategy_agreement"] == {
        "agreeing_strategies": 1,
        "total_strategies": 10,
        "used_as_weight": False,
    }
    assert result["trade_authorized"] is False


def test_performance_agent_reports_only_recorded_paper_outcomes():
    result = PerformanceLearningAgent().run([],[],[])
    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert result.metrics["paper_opened"] == 0
    assert result.metrics["win_rate"] is None
    assert result.metrics["net_paper_profit"] is None


def test_contract_agent_checks_exploratory_quote_without_approving_it():
    result=ContractIntelligenceAgent().run(
        {
            "eligible":False,
            "reason":"Exploratory quote review is not an approved paper signal.",
            "research_candidate":{
                "predicted_digit":7,
                "duration":1,
                "probability":"0.2",
                "stake":"1",
                "payout":"8",
                "expected_value":"0.6",
                "forecast_epoch":100,
                "duration_profitability_status":"RESEARCH_ONLY",
            },
        },
        {
            "digits":[{
                "digit":7,
                "durations":{
                    "1":{
                        "decision":"DO NOT TRADE",
                        "current_quote":{"valid":True,"spot_time":100},
                    },
                },
            }],
        },
    )

    assert result.status=="REJECTED"
    assert result.metrics["expected_value"]=="0.6"
    assert result.metrics["quote_spot_time"]==100
    assert any("Exploratory quote review" in reason for reason in result.reasons)


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


def test_agent_failure_is_recorded_and_remaining_agents_still_run(monkeypatch):
    from bromax import agents as agents_module

    def fail_digit_extraction(*args, **kwargs):
        raise RuntimeError("data agent test failure")

    monkeypatch.setattr(agents_module, "extract_last_digit", fail_digit_extraction)
    signals = [{
        "strategy_id":strategy.strategy_id,
        "predicted_digit":0,
        "predicted_digit_probability":0.1,
        "probabilities":[0.1]*10,
    } for strategy in candidate_strategies()]
    coordinated = coordinate_snapshot(
        report_item={},
        digit_analysis={
            "digits":[
                {"digit":digit,"estimated_win_probability":0.1,"health_percent":None}
                for digit in range(10)
            ],
            "volatility_filter":{"status":"INSUFFICIENT DATA"},
        },
        signal_data={
            "symbol":"1HZ100V",
            "status":"ready",
            "observed_tick_id":21,
            "observed_epoch":1_700_000_021,
            "signals":signals,
        },
        timing={"strategies":[]},
        ready={"status":"WAITING","eligible":False,"reason":"No approved quote-backed signal."},
        quality_ticks=_ticks(21),
        feed_stale=False,
        max_data_age_seconds=5,
        collector_errors=[],
        paper_events=[],
        forecasts=[],
        prior_runs=[],
        consistent=True,
    )

    results={item["agent_id"]:item for item in coordinated["agents"]}
    assert len(results)==8
    assert results["data_intelligence"]["status"]=="ERROR"
    assert "data agent test failure" in results["data_intelligence"]["reasons"][0]
    assert results["probability_calibration"]["status"]=="APPROVED"
    assert results["chief_intelligence_coordinator"]["status"]=="INSUFFICIENT_EVIDENCE"
    assert coordinated["decision"]["trade_authorized"] is False
    assert len(coordinated["coordination"]["handoffs"])==12
    assert any(
        item["from_agent"]=="probability_calibration"
        and item["to_agent"]=="risk_signal_approval"
        and item["result_status"]=="APPROVED"
        for item in coordinated["coordination"]["handoffs"]
    )
