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
