from bromax.timing import evaluate_payout_timing, _profit_summary


def test_no_quote_settlement_pairs_never_selects_duration():
    report = evaluate_payout_timing([])
    assert report['best_duration'] is None
    assert report['execution_authorized'] is False
    assert report['status'] == 'collecting_quote_backed_settlements'


def test_profit_summary_uses_actual_quoted_stake_and_gross_payout():
    rows = [
        {'stake': '1', 'payout': '9', 'hit': 1, 'duration_ticks': 1},
        {'stake': '1', 'payout': '9', 'hit': 0, 'duration_ticks': 1},
        {'stake': '2', 'payout': '18', 'hit': 1, 'duration_ticks': 1},
    ]
    summary = _profit_summary(rows)
    assert summary['total_stake'] == 4
    assert summary['total_pnl'] == 23  # +8 -1 +16
    assert summary['n'] == 3


def test_quote_settlement_must_be_exact_duration_epoch():
    quote = {'event_type':'QUOTE','forecast_seq':1,'duration_ticks':2,'strategy_id':'s',
             'forecast_epoch':100,'quote_spot_time':'100','stake':'1','payout':'9'}
    wrong = {'event_type':'SETTLE','forecast_seq':1,'duration_ticks':2,'strategy_id':'s',
             'forecast_epoch':100,'actual_epoch':103,'hit':1,'stake':'1','payout':'9'}
    report = evaluate_payout_timing([quote, wrong], min_holdout=1)
    assert report['best_duration'] is None
    assert report['status'] == 'collecting_quote_backed_settlements'


def test_optimizer_selects_only_quote_backed_profitable_holdout_candidate():
    events=[]
    for epoch in range(1,1001):
        seq=epoch
        events.append({'event_type':'QUOTE','forecast_seq':seq,'duration_ticks':1,
                       'strategy_id':'s','strategy_version':'1','forecast_epoch':epoch,
                       'forecast_tick_id':epoch,'prediction':7,'quote_spot_time':str(epoch),
                       'stake':'1','payout':'10','proposal_id':f'p{seq}'})
        hit=int(epoch%4==0)  # 25% wins; 10x gross payout gives positive realized EV.
        events.append({'event_type':'SETTLE','forecast_seq':seq,'duration_ticks':1,
                       'strategy_id':'s','strategy_version':'1','forecast_epoch':epoch,
                       'actual_epoch':epoch+1,'actual_digit':7 if hit else 3,'hit':hit,
                       'stake':'1','payout':'10','paper_pnl':'9' if hit else '-1'})
    report=evaluate_payout_timing(events,min_holdout=100)
    assert report['best_duration']==1
    assert report['best_candidate']['strategy_id']=='s'
    assert report['best_candidate']['final_test']['mean_return_ci95'][0] > 0
    assert report['execution_authorized'] is False
