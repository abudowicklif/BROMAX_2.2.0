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
