import pytest

def test_api_exposes_research_and_paper_routes_but_no_live_order_route(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("DATABASE_PATH",str(tmp_path/"api.sqlite3"))
    monkeypatch.setenv("DERIV_SYMBOLS","1HZ100V")
    import bromax.api as api_module
    api_module=importlib.reload(api_module)
    paths={route.path for route in api_module.app.routes}
    assert "/health" in paths
    assert "/report" in paths
    assert "/paper/trades" in paths
    assert "/intelligence" in paths
    assert "/signals" in paths
    assert "/signals/ready" in paths
    assert "/timing" in paths
    assert "/bridge/status" in paths
    assert "/dashboard" in paths
    assert "/advanced" in paths
    assert "/digits/analysis" in paths
    assert not any("buy" in path.lower() or "live" in path.lower() for path in paths)
    assert api_module.paper_status()["mode"]=="SIMULATED_ONLY"
    bridge=api_module.bridge_status()
    assert bridge["execution"]=="paper_only_no_buy_endpoint"
    assert bridge["mode"]=="SIMULATED_ONLY"
    page=api_module.dashboard()
    assert "BROMAX" in page
    assert "Best trading signal" in page
    assert "NO QUALIFIED SIGNAL" in page
    assert "Digit prediction" in page
    assert page.count("<section ")==5
    assert "Timing & volatility research" not in page
    advanced=api_module.advanced_dashboard()
    assert "Ready to trade" in advanced
    assert 'href="/dashboard"' in advanced
    ready=api_module.ready_signal("1HZ100V")
    assert ready["eligible"] is False
    assert ready["status"] == "WAITING"
    assert api_module.intelligence("1HZ100V",100)["signal_status"]=="RESEARCH_ONLY_NOT_APPROVED"
    assert api_module.signals("1HZ100V")["trade_authorized"] is False
    timing = api_module.timing_research("1HZ100V")
    assert timing["execution_authorized"] is False
    assert timing["durations_tested_ticks"] == [1, 2, 3, 4, 5]
    assert "payout_optimization" in timing
    assert timing["payout_optimization"]["execution_authorized"] is False

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
