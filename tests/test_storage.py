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
