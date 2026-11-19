import pytest

def test_api_exposes_research_and_paper_routes_but_no_live_order_route(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"api.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    paths={route.path for route in api_module.app.routes}
    assert "/health" in paths
    assert "/performance" in paths
    assert "/safety/kill-switch" in paths
    assert "/report" in paths
    assert "/paper/trades" in paths
    assert "/signals/history" in paths
    assert "/signals/primary" in paths
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
    assert "Primary research signal" in page
    assert "Last primary analysis" in page
    assert "primaryAnalysisStatus" in page
    assert "NO QUALIFIED SIGNAL" in page
    assert "QUALIFIED · RESEARCH ONLY" in page
    assert "Top-ranked digit" not in page
    assert "Strategy forecasts · latest tick" in page
    assert "Live analysis pipeline" in page
    assert "Forecast history · highest-probability candidate per tick" in page
    assert "signalHistory" in page
    assert page.count("<section ")==5
    assert "Paper trading results" not in page
    assert "Volatility check" not in page
    assert "HISTORY_REFRESH_MS=30000" in page
    assert "/signals?symbol=" in page
    assert "/signals/primary?symbol=" in page
    assert "&limit=100" in page
    assert "/health" in page
    assert "/decision?symbol=" not in page
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
    assert health["primary_analysis"]["1HZ100V"]["status"]=="WAITING_FOR_DATA"
    perf=api_module.performance()
    assert perf["symbols"]["1HZ100V"]["pipeline"]["performance"]["stages_ms"]["tick_to_forecast_ms"]["sample_count"]==0
    signal_history=api_module.signal_history("1HZ100V")
    assert signal_history["trade_authorized"] is False
    assert signal_history["signals"]==[]
    primary=api_module.primary_signal("1HZ100V")
    assert primary["status"]=="NO QUALIFIED SIGNAL"
    assert primary["digit"] is None
    assert primary["trade_authorized"] is False
    assert api_module.intelligence("1HZ100V",100)["signal_status"]=="RESEARCH_ONLY_NOT_APPROVED"
    assert api_module.signals("1HZ100V")["trade_authorized"] is False
    timing = api_module.timing_research("1HZ100V")
    assert timing["execution_authorized"] is False
    assert timing["durations_tested_ticks"] == [1, 2, 3, 4, 5]
    assert "payout_optimization" in timing
    assert timing["payout_optimization"]["execution_authorized"] is False


def test_background_primary_analysis_stops_at_failed_research_gate(monkeypatch,tmp_path):
    import importlib
    from datetime import datetime,timezone
    from bromax.strategies import candidate_strategies

    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"background-primary.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    received_at=datetime.now(timezone.utc).isoformat()
    ticks=[
        {
            "id":index,
            "epoch":1_700_000_000+index,
            "quote_text":"1234.56",
            "pip_size":2,
            "digit":6,
            "received_utc":received_at,
        }
        for index in range(1,22)
    ]
    signal_data={
        "symbol":"1HZ100V",
        "status":"ready",
        "observed_tick_id":21,
        "observed_epoch":1_700_000_021,
        "signals":[{"strategy_id":strategy.strategy_id} for strategy in candidate_strategies()],
    }
    monkeypatch.setattr(api_module.collector,"signal_snapshot",lambda symbol:signal_data)
    monkeypatch.setattr(api_module.collector,"last_received",{"1HZ100V":api_module.time.time()})
    monkeypatch.setattr(api_module.store,"quality_history",lambda symbol,limit:ticks)
    monkeypatch.setattr(api_module,"_load_primary_report_item",lambda symbol:{"symbol":symbol,"digit_evidence":{}})
    monkeypatch.setattr(
        api_module,
        "unified_decision",
        lambda symbol:pytest.fail("Full decision analysis must not run when research validation fails."),
    )

    attempted=api_module._run_primary_analysis_once("1HZ100V")

    assert attempted is True
    primary=api_module._primary_signal_cache["1HZ100V"]
    assert primary["status"]=="NO QUALIFIED SIGNAL"
    assert primary["digit"] is None
    assert primary["observed_tick_id"]==21
    assert any("No development-selected strategy" in reason for reason in primary["reasons"])
    assert api_module._primary_analysis_snapshot()["1HZ100V"]["status"]=="NO_QUALIFIED_SIGNAL"


def test_background_primary_worker_polls_at_bounded_interval(monkeypatch,tmp_path):
    import asyncio
    import importlib

    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"background-cadence.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    attempts=[]
    delays=[]
    monkeypatch.setattr(api_module,"_run_primary_analysis_once",lambda symbol:attempts.append(symbol) or True)

    async def fake_to_thread(function,*args):
        return function(*args)

    async def fake_sleep(delay):
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(api_module.asyncio,"to_thread",fake_to_thread)
    monkeypatch.setattr(api_module.asyncio,"sleep",fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(api_module._primary_signal_worker())

    assert attempts==["1HZ100V"]
    assert delays==[api_module._PRIMARY_ANALYSIS_POLL_SECONDS]


def test_decision_returns_service_unavailable_during_background_analysis(monkeypatch,tmp_path):
    import importlib
    from fastapi import HTTPException

    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"decision-lock.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    api_module._decision_analysis_lock.acquire()
    try:
        with pytest.raises(HTTPException) as error:
            api_module.unified_decision("1HZ100V")
        assert error.value.status_code==503
    finally:
        api_module._decision_analysis_lock.release()


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
    assert first["coordination"]["status"] == "INCONSISTENT_SNAPSHOT"
    assert first["coordination"]["selection_ready"] is False
    assert first["coordination"]["selection"] == "NO QUALIFIED SIGNAL"
    assert first["primary_signal"]["status"] == "NO QUALIFIED SIGNAL"
    assert first["primary_signal"]["digit"] is None
    assert second["agent_run_persisted"] is False
    assert len(api_module.store.agent_runs("1HZ100V"))==1
    assert {agent["evaluated_at"] for agent in first["agents"]}=={first["created_at"]}


def test_primary_signal_route_serves_only_a_fresh_matching_cache(monkeypatch,tmp_path):
    import importlib
    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"primary-signal.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    monkeypatch.setenv("MAX_DATA_AGE_SECONDS","5")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    monkeypatch.setattr(api_module.time,"time",lambda:100.0)
    monkeypatch.setattr(api_module.collector,"signal_snapshot",lambda symbol:{
        "symbol":symbol,"status":"ready","observed_tick_id":17,"observed_epoch":1700000017,
    })
    api_module.collector.last_received["1HZ100V"]=99.0
    cached={
        "status":"QUALIFIED","symbol":"1HZ100V","digit":4,
        "observed_tick_id":17,"observed_epoch":1700000017,
        "trade_authorized":False,
    }
    api_module._primary_signal_cache["1HZ100V"]=cached

    assert api_module.primary_signal("1HZ100V") is cached

    api_module.collector.last_received["1HZ100V"]=90.0
    stale=api_module.primary_signal("1HZ100V")
    assert stale["status"]=="NO QUALIFIED SIGNAL"
    assert stale["digit"] is None
    assert stale["trade_authorized"] is False


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

def test_pipeline_forecast_count_cache_refreshes_after_expiry(monkeypatch,tmp_path):
    import importlib
    from bromax.strategies import Forecast

    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"pipeline-counts.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    tick_id=api_module.store.add_tick("1HZ100V",1700000000,"1234.56",2,6)
    probabilities=(.1,.1,.1,.4,.1,.05,.05,.05,.025,.025)
    first=Forecast("first","test",{},3,probabilities)
    second=Forecast("second","test",{},3,probabilities)

    api_module.store.append_forecast("1HZ100V",first,tick_id,1700000000)
    assert api_module._stored_forecast_count("1HZ100V")==1

    api_module.store.append_forecast("1HZ100V",second,tick_id,1700000000)
    assert api_module._stored_forecast_count("1HZ100V")==1

    api_module._pipeline_forecast_count_cache["1HZ100V"]=(
        api_module.time.monotonic()-api_module._PIPELINE_FORECAST_COUNT_TTL_SECONDS-1,1
    )
    assert api_module._stored_forecast_count("1HZ100V")==2

def test_signals_delivers_only_complete_persisted_forecasts_for_latest_tick(monkeypatch,tmp_path):
    import importlib
    from bromax.strategies import Forecast

    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"persisted-signals.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    with api_module.store.connect() as connection:
        connection.executemany(
            "INSERT INTO ticks(symbol,epoch,quote_text,pip_size,digit,received_utc) VALUES(?,?,?,?,?,?)",
            [
                ("1HZ100V",1_700_000_000+index,f"1234.{index%100:02d}",2,index%10,"2026-01-01T00:00:00+00:00")
                for index in range(1000)
            ],
        )
    latest=api_module.store.history("1HZ100V",limit=1)[-1]
    forecasts=[]
    for index,strategy in enumerate(api_module.collector.strategies):
        prediction=index
        probabilities=[.01]*10
        probabilities[prediction]=.91
        forecasts.append(Forecast(strategy.strategy_id,strategy.version,dict(strategy.parameters),
                                  prediction,tuple(probabilities)))
    api_module.store.append_forecasts(
        "1HZ100V",forecasts,latest["id"],latest["epoch"],note="PAYOUT_PENDING"
    )
    api_module.collector=api_module.MarketCollector(
        api_module.settings,api_module.store,api_module.paperbot
    )

    def unexpected_query(*args,**kwargs):
        raise AssertionError("current signals must not query SQLite")

    with monkeypatch.context() as patch:
        patch.setattr(api_module.store,"history",unexpected_query)
        patch.setattr(api_module.store,"recent_forecasts",unexpected_query)
        result=api_module.signals("1HZ100V")
    assert result["observed_tick_id"]==latest["id"]
    assert result["observed_epoch"]==latest["epoch"]
    assert len(result["signals"])==len(forecasts)
    assert {row["strategy_id"] for row in result["signals"]}=={
        strategy.strategy_id for strategy in api_module.collector.strategies
    }
    assert all(row["status"]=="RESEARCH_ONLY_NOT_APPROVED" for row in result["signals"])
    assert result["trade_authorized"] is False

    newer_id=api_module.store.add_tick(
        "1HZ100V",int(latest["epoch"])+1,"1234.57",2,7,"next-tick"
    )
    api_module.collector._refresh_recent_history("1HZ100V")
    pending=api_module.signals("1HZ100V")
    assert pending["status"]=="analysis_pending"
    assert pending["observed_tick_id"]==newer_id
    assert pending["signals"]==[]
    assert pending["trade_authorized"] is False


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
