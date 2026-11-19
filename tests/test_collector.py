import asyncio
import json
import time
from decimal import Decimal

import pytest

from bromax.collector import MarketCollector
from bromax.config import Settings
from bromax.storage import Store
from bromax.strategies import Forecast


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
    timings=collector.performance_metrics("1HZ100V")["stages_ms"]
    assert timings["message_parse_ms"]["sample_count"]==0
    for stage in ("data_processing_ms","strategy_evaluation_ms","signal_persistence_ms",
                  "collector_processing_ms","tick_to_forecast_ms"):
        assert timings[stage]["sample_count"]==1
        assert timings[stage]["max"]>=0


@pytest.mark.asyncio
async def test_optional_quote_persistence_does_not_block_tick_event_loop(monkeypatch,tmp_path):
    collector,store=_collector(tmp_path)
    epoch=int(time.time())+60
    tick_id=store.add_tick("1HZ100V",epoch,"1234.56",2,6,"quote-test")
    forecast=Forecast("quote-test","1.0",{},6,(.1,.1,.1,.1,.1,.1,.4,.1,.1,.1))
    forecast_seq=store.append_forecast("1HZ100V",forecast,tick_id,epoch)

    class FakeProposals:
        active=0
        maximum_active=0

        async def quote_many(self,requests):
            self.active+=1
            self.maximum_active=max(self.maximum_active,self.active)
            try:
                await asyncio.sleep(.01)
                return {
                    (symbol,digit,duration):{
                        "payout":Decimal("9"),
                        "stake":Decimal("1"),
                        "proposal_id":f"{digit}-{duration}",
                        "spot_time":epoch,
                    }
                    for symbol,digit,_stake,duration in requests
                }
            finally:
                self.active-=1

    proposals=FakeProposals()
    collector.proposals=proposals
    append_duration_quote=store.append_duration_quote

    def slow_append_duration_quote(*args,**kwargs):
        time.sleep(.03)
        return append_duration_quote(*args,**kwargs)

    monkeypatch.setattr(store,"append_duration_quote",slow_append_duration_quote)
    captures=[
        asyncio.create_task(
            collector._capture_quotes("1HZ100V",tick_id,epoch,[(forecast,forecast_seq)])
        )
        for _ in range(4)
    ]

    async def verify_loop_responsive():
        await asyncio.sleep(.01)
        assert any(not capture.done() for capture in captures)

    await asyncio.gather(*captures,verify_loop_responsive())
    assert proposals.maximum_active<=3
    assert len(store.duration_events("1HZ100V"))==5
    assert store.paper_events()==[]


@pytest.mark.asyncio
async def test_duplicate_invalid_and_out_of_order_ticks_are_counted_and_dropped(tmp_path):
    epoch=1_700_000_000
    settings=Settings(database_path=str(tmp_path/"collector.sqlite3"))
    store=Store(settings.database_path)
    store.add_tick("1HZ100V",epoch,"1234.50",2,0)
    collector=MarketCollector(settings,store)
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
    assert [row["epoch"] for row in collector._recent_history["1HZ100V"]]==[
        1700000001,1700000002,1700000003
    ]
    assert [row["epoch"] for row in store.history("1HZ100V")]==[1700000001,1700000002,1700000003]
    assert collector._seed_history(
        "1HZ100V",
        {"prices":[Decimal("1.10"),Decimal("1.14")],"times":[1700000000,1700000004]},
        2,
    )==2
    assert [row["epoch"] for row in store.history("1HZ100V")]==[
        1700000000,1700000001,1700000002,1700000003,1700000004
    ]
    assert [row["epoch"] for row in collector._recent_history["1HZ100V"]]==[
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
