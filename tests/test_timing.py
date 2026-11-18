from bromax.timing import evaluate_timing, wilson


def test_wilson_bounds():
    lo, hi = wilson(10, 100)
    assert 0 < lo < .1 < hi < 1


def test_timing_uses_future_horizon_and_never_authorizes_execution():
    ticks = [
        {'id': i+1, 'epoch': 1000+i, 'digit': i % 10, 'quote_text': f'{100+i/100:.2f}', 'symbol': '1HZ100V'}
        for i in range(80)
    ]
    forecasts = [{'event_type':'FORECAST','forecast_tick_id':i+1,'prediction':(i+1)%10,'strategy_id':'test'} for i in range(70)]
    result = evaluate_timing(ticks, forecasts, durations=(1,2,3))
    assert result['execution_authorized'] is False
    assert result['durations_tested_ticks'] == [1,2,3]
    assert {x['duration_ticks'] for x in result['strategies']} == {1,2,3}
    assert result['best_duration'] is None


def test_missing_epoch_gap_is_not_scored():
    ticks = [
        {'id':1,'epoch':100,'digit':1,'quote_text':'1.1'},
        {'id':2,'epoch':101,'digit':2,'quote_text':'1.2'},
        {'id':3,'epoch':103,'digit':3,'quote_text':'1.3'},
        {'id':4,'epoch':104,'digit':4,'quote_text':'1.4'},
    ]
    forecasts = [{'forecast_tick_id':1,'prediction':2,'strategy_id':'s'}]
    result = evaluate_timing(ticks, forecasts, durations=(1,2))
    rows = {x['duration_ticks']:x for x in result['strategies']}
    assert rows[1]['n'] == 1
    assert 2 not in rows


def test_volatility_gate_is_fail_closed_without_holdout_evidence():
    ticks = [
        {'id': i+1, 'epoch': 2000+i, 'digit': (i*7)%10,
         'quote_text': f'{100+i/100:.2f}', 'symbol': '1HZ100V'}
        for i in range(120)
    ]
    forecasts = [
        {'forecast_tick_id': i+1, 'prediction': (i*3)%10, 'strategy_id': 'candidate'}
        for i in range(100)
    ]
    result = evaluate_timing(ticks, forecasts, durations=(1,))
    assert result['execution_authorized'] is False
    row = result['strategies'][0]
    assert all(x['status'] == 'UNCONFIRMED' for x in row['volatility_regimes'])
