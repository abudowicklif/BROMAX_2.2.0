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
