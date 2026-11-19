from datetime import datetime, timedelta, timezone

from bromax.agents import (
    AgentResult,
    ChiefIntelligenceCoordinator,
    DataIntelligenceAgent,
    PerformanceLearningAgent,
    ProbabilityCalibrationAgent,
    _strategy_batch_summary,
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
