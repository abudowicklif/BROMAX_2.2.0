"""Causal multi-horizon MATCH timing and volatility diagnostics.

This module deliberately does not authorize execution. A duration is only a
research candidate until duration-specific Deriv proposal quotes, untouched
out-of-sample validation, and forward paper settlement are available.
"""
from __future__ import annotations
from collections import defaultdict
import math
import numpy as np
from scipy.stats import binomtest, norm, t

DURATIONS = (1, 2, 3, 4, 5)
MIN_OBSERVATIONS = 5000
MIN_VOL_BUCKET_OBSERVATIONS = 1000


def wilson(k: int, n: int, z: float = 1.959963984540054):
    if n <= 0:
        return None
    p = k / n
    den = 1 + z*z/n
    center = (p + z*z/(2*n))/den
    half = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/den
    return [max(0.0, center-half), min(1.0, center+half)]


def _volatility_features(ticks):
    """Trailing 20-return mean absolute return, computed using past/current prices only."""
    prices=[]
    for row in ticks:
        try:
            prices.append(float(row['quote_text']))
        except (ValueError, TypeError, KeyError):
            prices.append(float('nan'))
    returns=[None]
    for a,b in zip(prices[:-1],prices[1:]):
        returns.append(abs((b-a)/a) if math.isfinite(a) and math.isfinite(b) and a else None)
    result=[]
    for i in range(len(ticks)):
        sample=[x for x in returns[max(1,i-19):i+1] if x is not None and math.isfinite(x)]
        result.append(float(np.mean(sample)) if len(sample)>=10 else None)
    return result


def _holm(pvalues):
    if not pvalues:
        return []
    order=sorted(range(len(pvalues)),key=lambda i:pvalues[i])
    adjusted=[1.0]*len(pvalues); running=0.0; m=len(pvalues)
    for rank,index in enumerate(order):
        running=max(running,min(1.0,(m-rank)*pvalues[index]))
        adjusted[index]=running
    return adjusted


def _summary(rows):
    n=len(rows); k=sum(x[0] for x in rows); ci=wilson(k,n)
    p=float(binomtest(k,n,.10,alternative='greater').pvalue) if n else 1.0
    return {'n':n,'hits':k,'hit_rate':k/n if n else None,'hit_rate_ci95':ci,'p_vs_10pct':p}


def evaluate_timing(ticks, forecasts, durations=DURATIONS):
    """Score frozen predictions at N ticks ahead with chronological holdouts.

    Duration candidates are selected on validation, then tested once on the
    untouched final segment. A positive hit-rate result still is not tradable
    without actual duration-specific proposal quotes and payout-adjusted EV.
    """
    ticks=[dict(x) for x in ticks]
    if not ticks:
        return {'status':'insufficient_data','symbol':None,'durations_tested_ticks':list(durations),'strategies':[],'best_duration':None,'execution_authorized':False,'limitations':['Insufficient tick history; no timing or volatility conclusion is available.']}
    by_id={int(t['id']):i for i,t in enumerate(ticks)}
    vol=_volatility_features(ticks)
    valid_train=[x for x in vol[:max(1,int(len(vol)*.60))] if x is not None]
    cuts=np.quantile(valid_train,[1/3,2/3]).tolist() if len(valid_train)>=30 else None
    samples=defaultdict(list)
    for f in forecasts:
        idx=by_id.get(int(f.get('forecast_tick_id') or -1))
        if idx is None or f.get('prediction') is None:
            continue
        for h in durations:
            target=idx+int(h)
            if target>=len(ticks) or int(ticks[target]['epoch'])-int(ticks[idx]['epoch']) != int(h):
                continue
            hit=int(int(f['prediction'])==int(ticks[target]['digit']))
            samples[(f.get('strategy_id','unknown'),int(h))].append((idx,hit,vol[idx]))
    n_ticks=len(ticks); dev_end=int(n_ticks*.60); val_end=int(n_ticks*.80)
    # Build validation family once, then adjust across every strategy-duration pair.
    validation={key:_summary([(hit,v) for idx,hit,v in rows if dev_end<=idx<val_end]) for key,rows in samples.items()}
    keys=list(validation)
    adjusted=_holm([validation[k]['p_vs_10pct'] for k in keys])
    for key,padj in zip(keys,adjusted): validation[key]['holm_adjusted_p']=padj
    gates={k for k in keys if validation[k]['n']>=MIN_OBSERVATIONS and validation[k]['hit_rate_ci95'] and validation[k]['hit_rate_ci95'][0]>.10 and validation[k]['holm_adjusted_p']<.05}
    test={key:_summary([(hit,v) for idx,hit,v in rows if idx>=val_end]) for key,rows in samples.items() if key in gates}
    test_keys=list(test); test_adjusted=_holm([test[k]['p_vs_10pct'] for k in test_keys])
    for key,padj in zip(test_keys,test_adjusted): test[key]['holm_adjusted_p']=padj
    def regime_name(value):
        if cuts is None or value is None: return None
        return 'LOW' if value<=cuts[0] else ('MID' if value<=cuts[1] else 'HIGH')
    regime_samples=defaultdict(list)
    for (sid,h),rows in samples.items():
        for idx,hit,value in rows:
            regime=regime_name(value)
            if regime: regime_samples[(sid,h,regime)].append((idx,hit,value))
    regime_validation={key:_summary([(hit,v) for idx,hit,v in rows if dev_end<=idx<val_end]) for key,rows in regime_samples.items()}
    regime_keys=list(regime_validation)
    regime_adj=_holm([regime_validation[k]['p_vs_10pct'] for k in regime_keys])
    for key,padj in zip(regime_keys,regime_adj): regime_validation[key]['holm_adjusted_p']=padj
    regime_gates={key for key,summary in regime_validation.items()
                  if summary['n']>=MIN_VOL_BUCKET_OBSERVATIONS and summary['hit_rate_ci95']
                  and summary['hit_rate_ci95'][0]>.10 and summary['holm_adjusted_p']<.05}
    regime_test={key:_summary([(hit,v) for idx,hit,v in rows if idx>=val_end])
                 for key,rows in regime_samples.items() if key in regime_gates}
    regime_test_keys=list(regime_test)
    regime_test_adj=_holm([regime_test[k]['p_vs_10pct'] for k in regime_test_keys])
    for key,padj in zip(regime_test_keys,regime_test_adj): regime_test[key]['holm_adjusted_p']=padj
    strategies=[]
    for (sid,h),rows in sorted(samples.items()):
        all_hits=[(hit,v) for _,hit,v in rows]
        summary=_summary(all_hits); val=validation.get((sid,h),_summary([])); final=test.get((sid,h))
        buckets=[]
        for name in ('LOW','MID','HIGH'):
            key=(sid,h,name); bucket_rows=regime_samples.get(key,[])
            bv=regime_validation.get(key,_summary([])); bt=regime_test.get(key)
            confirmed=bool(bv['n']>=MIN_VOL_BUCKET_OBSERVATIONS and bv['hit_rate_ci95'] and bv['hit_rate_ci95'][0]>.10 and bv.get('holm_adjusted_p',1)<.05 and bt and bt['n']>=MIN_VOL_BUCKET_OBSERVATIONS and bt['hit_rate_ci95'] and bt['hit_rate_ci95'][0]>.10 and bt['holm_adjusted_p']<.05)
            buckets.append({'regime':name,'n':len(bucket_rows),'hits':sum(x[1] for x in bucket_rows),
                            'validation':bv,'final_test':bt,
                            'status':'CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON' if confirmed else 'UNCONFIRMED'})
        final_ok=bool(final and final['n']>=MIN_OBSERVATIONS and final['hit_rate_ci95'] and final['hit_rate_ci95'][0]>.10 and final['holm_adjusted_p']<.05)
        strategies.append({'strategy_id':sid,'duration_ticks':h,**summary,
                           'development':_summary([(hit,v) for idx,hit,v in rows if idx<dev_end]),
                           'validation':val,'final_test':final,
                           'timing_status':'HIT_RATE_EDGE_ONLY_REQUIRES_QUOTE_EV' if final_ok else 'RESEARCH_ONLY',
                           'volatility_regimes':buckets})
    eligible=[x for x in strategies if x['timing_status']=='HIT_RATE_EDGE_ONLY_REQUIRES_QUOTE_EV']
    best=max(eligible,key=lambda x:(x['final_test']['hit_rate_ci95'][0],x['final_test']['hit_rate']),default=None)
    valid_vol=[x for x in vol if x is not None]
    current_volatility=vol[-1] if vol else None
    short_volatility=float(np.mean(valid_vol[-20:])) if valid_vol else None
    long_volatility=float(np.mean(valid_vol[-200:])) if valid_vol else None
    return {'status':'research_only','symbol':ticks[-1].get('symbol'),
            'durations_tested_ticks':list(durations),'minimum_observations_per_holdout':MIN_OBSERVATIONS,
            'chronological_splits':{'development':'first 60%','validation':'next 20%','final_test':'last 20%'},
            'volatility_cutoffs_training_terciles':cuts,'current_volatility_regime':regime_name(vol[-1] if vol else None),'strategies':strategies,
            'current_volatility':current_volatility,'short_term_volatility':short_volatility,
            'long_term_volatility':long_volatility,
            'best_research_candidate':best,'best_duration':best['duration_ticks'] if best else None,
            'execution_authorized':False,
            'limitations':['Predictions are frozen per-tick strategy forecasts scored at future horizons; models are not separately retrained for each duration.',
              'Final-test selection uses hit-rate significance only; no duration-specific historical Deriv proposal quotes are stored, so payout-adjusted expected value is unverified.',
              'Volatility confirmation is strategy- and horizon-specific, requires at least 1,000 untouched final-test observations per regime and Holm-adjusted significance; even then it is not proof of a correct next digit.',
              'A duration cannot be enabled until its own quote capture, payout-adjusted EV, untouched test and forward paper settlement pass.',
              'The existing 1-tick paper executor remains unchanged. No live orders are possible.']}


def _profit_summary(rows):
    """Quote-backed gross-return P&L summary; each row has stake and payout."""
    values=[]
    for row in rows:
        try:
            stake=float(row['stake']); payout=float(row['payout']); hit=int(row['hit'])
            if stake>0 and math.isfinite(stake) and math.isfinite(payout):
                pnl=(payout-stake) if hit else -stake
                values.append((pnl,stake,pnl/stake))
        except (TypeError,ValueError,KeyError):
            continue
    n=len(values)
    if not n: return {'n':0,'wins':0,'hit_rate':None,'total_stake':0.0,'total_pnl':0.0,'mean_pnl_per_contract':None,'mean_return_on_stake':None,'mean_return_ci95':None,'p_value_profit_gt_zero':1.0}
    returns=np.asarray([x[2] for x in values],dtype=float)
    pnls=np.asarray([x[0] for x in values],dtype=float)
    if n>1 and np.std(returns,ddof=1)>0:
        # Newey-West/HAC standard error: overlapping 1-5 tick contracts and
        # serial dependence otherwise make the naive IID interval too narrow.
        centered=returns-float(np.mean(returns)); gamma0=float(np.dot(centered,centered)/n)
        horizon=max(1,int(rows[0].get('duration_ticks',1)))
        lag=5
        for k in range(1,lag+1):
            gamma=float(np.dot(centered[k:],centered[:-k])/n)
            gamma0 += 2.0*(1.0-k/(lag+1))*gamma
        se=float(math.sqrt(max(gamma0,0.0)/n)); crit=float(norm.ppf(.975))
        ci=[float(np.mean(returns)-crit*se),float(np.mean(returns)+crit*se)] if se>0 else None
        p=float(norm.sf(float(np.mean(returns))/se)) if se>0 else 1.0
    else:
        ci=None; p=1.0
    return {'n':n,'wins':sum(x[0]>0 for x in values),'hit_rate':sum(x[0]>0 for x in values)/n,
            'total_stake':float(sum(x[1] for x in values)),'total_pnl':float(np.sum(pnls)),
            'mean_pnl_per_contract':float(np.mean(pnls)),'mean_return_on_stake':float(np.mean(returns)),
            'mean_return_ci95':ci,'p_value_profit_gt_zero':p}


def evaluate_payout_timing(duration_events, min_holdout=5000):
    """Select a duration only from real, contemporaneous Deriv quotes and settled outcomes.

    Quotes are frozen before their target tick; contracts are settled only at the
    exact target epoch. Validation selects candidates; final test is untouched.
    """
    quotes={}
    settled={}
    for row in duration_events:
        key=(int(row.get('forecast_seq') or 0),int(row.get('duration_ticks') or 0))
        if row.get('event_type')=='QUOTE': quotes[key]=row
        elif row.get('event_type')=='SETTLE': settled[key]=row
    pairs=[]
    for key,q in quotes.items():
        outcome=settled.get(key)
        if not outcome: continue
        # Never use a quote with a mismatched spot tick or an outcome at wrong epoch.
        if str(q.get('quote_spot_time')) != str(q.get('forecast_epoch')): continue
        if int(outcome.get('actual_epoch') or -1) != int(q['forecast_epoch'])+int(q['duration_ticks']): continue
        pairs.append({**q,**outcome,'event_type':'SETTLED'})
    if not pairs:
        return {'status':'collecting_quote_backed_settlements','durations_tested_ticks':[1,2,3,4,5],
                'minimum_observations_per_holdout':min_holdout,'strategies':[],'best_duration':None,
                'best_candidate':None,'execution_authorized':False,
                'note':'No complete duration-specific quote and settlement pairs yet. Quotes and outcomes must accrue prospectively; historical prices cannot recreate past Deriv payouts.'}
    pairs.sort(key=lambda x:(int(x['forecast_epoch']),int(x['forecast_seq'])))
    epochs=sorted({int(x['forecast_epoch']) for x in pairs})
    cut1=epochs[int(len(epochs)*.60)] if len(epochs)>2 else 10**30
    cut2=epochs[int(len(epochs)*.80)] if len(epochs)>2 else 10**30
    grouped={}
    for row in pairs: grouped.setdefault((row['strategy_id'],int(row['duration_ticks'])),[]).append(row)
    validation={k:_profit_summary([r for r in rows if cut1<=int(r['forecast_epoch'])<cut2]) for k,rows in grouped.items()}
    keys=list(validation); adjusted=_holm([validation[k]['p_value_profit_gt_zero'] for k in keys])
    for k,padj in zip(keys,adjusted): validation[k]['holm_adjusted_p']=padj
    finalists={k for k,v in validation.items() if v['n']>=min_holdout and v['mean_return_ci95'] and v['mean_return_ci95'][0]>0 and v['holm_adjusted_p']<.05}
    final={k:_profit_summary([r for r in rows if int(r['forecast_epoch'])>=cut2]) for k,rows in grouped.items() if k in finalists}
    fkeys=list(final); fadj=_holm([final[k]['p_value_profit_gt_zero'] for k in fkeys])
    for k,padj in zip(fkeys,fadj): final[k]['holm_adjusted_p']=padj
    output=[]
    for (sid,h),rows in sorted(grouped.items()):
        dev=_profit_summary([r for r in rows if int(r['forecast_epoch'])<cut1])
        val=validation[(sid,h)]; test=final.get((sid,h))
        passed=bool(test and test['n']>=min_holdout and test['mean_return_ci95'] and test['mean_return_ci95'][0]>0 and test.get('holm_adjusted_p',1)<.05)
        output.append({'strategy_id':sid,'duration_ticks':h,'all_settled':_profit_summary(rows),
                       'development':dev,'validation':val,'final_test':test,
                       'payout_ev_status':'OUT_OF_SAMPLE_PROFIT_EVIDENCE' if passed else 'RESEARCH_ONLY'})
    eligible=[x for x in output if x['payout_ev_status']=='OUT_OF_SAMPLE_PROFIT_EVIDENCE']
    best=max(eligible,key=lambda x:x['final_test']['mean_return_ci95'][0],default=None)
    return {'status':'quote_backed_research','durations_tested_ticks':[1,2,3,4,5],
            'minimum_observations_per_holdout':min_holdout,
            'chronological_splits':{'development':'first 60%','validation':'next 20%','final_test':'last 20%'},
            'quote_settled_pairs':len(pairs),'strategies':output,'best_candidate':best,
            'best_duration':best['duration_ticks'] if best else None,
            'execution_authorized':False,
            'limitations':['Expected value is calculated from actual quoted gross payout and stake, then realized against the exact future digit.','Only prospective quote-and-settlement pairs count; historical tick data cannot reconstruct past proposal prices.','A positive result is historical evidence, not a guarantee. Forward paper operation and risk approval remain mandatory.','No live contract purchase is enabled.']}
