from __future__ import annotations
import math
import numpy as np
import pandas as pd
from scipy.stats import binomtest
from .strategies import candidate_strategies, ForecastContext, Forecast, CumulativeMode

PROTOCOL_VERSION = "1.1.0"

PRE_REGISTERED = {
    "protocol_version": PROTOCOL_VERSION,
    "candidate_ids": [s.strategy_id for s in candidate_strategies()],
    "strategy_versions": {s.strategy_id:s.version for s in candidate_strategies()},
    "minimum_history_ticks": 1000,
    "contract_type": "DIGITMATCH",
    "contract_duration_ticks": 1,
    "split": {"development": 0.60, "validation": 0.20, "untouched_test": 0.20},
    "windows": [100, 500, 1000],
    "laplace_alpha": [1.0],
    "exponential_half_lives_ticks": [50, 200],
    "conditional_pattern_candidate": {"strategy_id": "conditional_transition_order2", "order": 2, "alpha": 1.0, "minimum_support_for_interpretation": 20, "low_support_fallback": "laplace_frequency_500"},
    "tie_break": "lowest digit",
    "selection": "rank development by multiclass Brier; gate on validation only",
    "validation_gates": {"minimum_n": 5000, "minimum_quoted_n": 5000, "minimum_quote_coverage": 0.90, "Holm_adjusted_one_sided_p_lt": 0.05, "hit_rate_block_CI95_lower_gt": 0.10, "realized_profit_CI95_lower_gt": 0.0},
    "multiple_comparison_scope": "Holm correction per symbol across the pre-registered strategy family",
    "final_test": "evaluate only validation finalists; no tuning or reselection on final test",
}

def wilson(k, n, z=1.959963984540054):
    if n == 0: return (None, None)
    p=k/n; den=1+z*z/n; centre=(p+z*z/(2*n))/den
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return centre-half,centre+half

def holm_adjust(pvalues):
    p=np.asarray(pvalues,dtype=float); m=len(p); order=np.argsort(p); out=np.empty(m); running=0.0
    for rank,idx in enumerate(order):
        running=max(running,(m-rank)*p[idx]); out[idx]=min(1.0,running)
    return out.tolist()

def reliability_table(probabilities, actuals):
    if not probabilities: return []
    bins=[0,.10,.15,.20,.30,.40,.50,.60,.70,.80,.90,1.000001]; rows=[]
    for lo,hi in zip(bins[:-1],bins[1:]):
        sub=[(p,a) for p,a in zip(probabilities,actuals) if lo <= max(p) < hi]
        if sub:
            rows.append({"top_probability_bin":f"[{lo:.2f},{hi:.2f})","n":len(sub),"mean_predicted_top_probability":float(np.mean([max(p) for p,a in sub])),"observed_top_digit_hit_rate":float(np.mean([int(np.argmax(p)==a) for p,a in sub]))})
    return rows

def _forecast_frame(ticks: pd.DataFrame, symbol: str):
    """Fast, causal batch replay. Each state is updated with tick i before forecasting i+1."""
    from collections import deque
    from .strategies import UniformBaseline
    records=[]
    digits=ticks.digit.astype(int).tolist(); ids=ticks.id.astype(int).tolist(); epochs=ticks.epoch.astype(int).tolist()
    windows=(100,500,1000)
    queues={w:deque() for w in windows}; counts={w:np.zeros(10,dtype=np.int64) for w in windows}
    half_lives=(50.0,200.0); decays={h:math.exp(-math.log(2)/h) for h in half_lives}; exp_counts={h:np.zeros(10,dtype=float) for h in half_lives}; exp_queues={h:deque() for h in half_lives}
    baseline=UniformBaseline(); cumulative_counts=np.zeros(10,dtype=np.int64); transition_counts={}; transition_events=deque()
    for i,digit in enumerate(digits[:-1]):
        cumulative_counts[digit]+=1
        for w in windows:
            q=queues[w]; q.append(digit); counts[w][digit]+=1
            if len(q)>w: counts[w][q.popleft()]-=1
        for h in half_lives:
            exp_counts[h] *= decays[h]; exp_counts[h][digit] += 1.0
            exp_queues[h].append(digit)
            if len(exp_queues[h])>1000:
                expired=exp_queues[h].popleft(); exp_counts[h][expired] -= decays[h]**1000
        if i >= 2:
            prior_pair = (digits[i-2], digits[i-1])
            if len(transition_events) >= 998:
                expired_pair, expired_digit = transition_events.popleft()
                transition_counts[expired_pair][expired_digit] -= 1
            transition_events.append((prior_pair, digit))
            transition_counts.setdefault(prior_pair, np.zeros(10, dtype=np.int64))[digit] += 1
        ctx=ForecastContext(symbol,ids[i],epochs[i],tuple(digits[:i+1]),tuple(int(x) for x in cumulative_counts))
        if i+1 < 1000: continue
        forecasts=[]
        f=baseline.forecast(ctx)
        forecasts.append(f)
        forecasts.append(CumulativeMode().forecast(ctx))
        for w in windows:
            c=counts[w].astype(float); probs=c/c.sum()
            forecasts.append(Forecast(f"rolling_mode_{w}","1.0.0",{"window":w,"tie_break":"lowest_digit"},int(np.argmax(probs)),tuple(probs)))
        for w in (100,500):
            c=counts[w].astype(float); probs=(c+1.0)/(c.sum()+10.0)
            forecasts.append(Forecast(f"laplace_freq_{w}_a1", "1.0.0",{"window":w,"alpha":1.0,"prior":"symmetric_dirichlet"},int(np.argmax(probs)),tuple(probs)))
        for h in half_lives:
            c=exp_counts[h]; probs=(c+1.0)/(c.sum()+10.0)
            forecasts.append(Forecast(f"exp_frequency_hl{h:g}","1.0.0",{"half_life_ticks":h,"weight":"exp(-ln(2)*age/half_life)","alpha":1.0},int(np.argmax(probs)),tuple(probs)))
        pair = (digits[i-1], digits[i]) if i >= 1 else None
        tc = transition_counts.get(pair, np.zeros(10, dtype=np.int64)) if pair is not None else np.zeros(10, dtype=np.int64)
        conditional_source = tc.astype(float) if tc.sum() >= 20 else counts[500].astype(float)
        cond_probs = (conditional_source+1.0)/(conditional_source.sum()+10.0)
        forecasts.append(Forecast("conditional_transition_order2", "1.0.0", {"order":2,"alpha":1.0,"minimum_support_for_interpretation":20,"low_support_fallback":"laplace_frequency_500"}, int(np.argmax(cond_probs)), tuple(float(x) for x in cond_probs)))
        # Only after every forecast is fixed do we inspect the next tick for scoring.
        if epochs[i+1] != epochs[i] + 1: continue
        actual_digit=digits[i+1]
        for f in forecasts:
            records.append({"symbol":symbol,"tick_index":i,"forecast_tick_id":ids[i],"epoch":epochs[i],"strategy_id":f.strategy_id,"version":f.version,"prediction":f.prediction,"actual":actual_digit,"hit":int(f.prediction==actual_digit),"probabilities":f.probabilities})
    frame=pd.DataFrame(records)
    if not frame.empty:
        n=len(ticks); dev=int(n*.60); val=int(n*.80)
        frame["split"]=np.where(frame.tick_index<dev,"development",np.where(frame.tick_index<val,"validation","final_test"))
        frame["volatility_regime"]=_volatility_regimes(ticks,frame.tick_index.tolist())
    return frame

def _volatility_regimes(ticks: pd.DataFrame, tick_indices):
    if "quote_text" not in ticks or ticks.empty:
        return [None for _ in tick_indices]
    prices=pd.to_numeric(ticks["quote_text"],errors="coerce").to_numpy(dtype=float)
    returns=np.full(len(prices),np.nan)
    if len(prices)>1:
        previous=prices[:-1]; current=prices[1:]
        valid=np.isfinite(previous)&np.isfinite(current)&(previous!=0)
        returns[1:][valid]=np.abs((current[valid]-previous[valid])/previous[valid])
    volatility=np.full(len(prices),np.nan)
    for i in range(len(prices)):
        sample=returns[max(1,i-19):i+1]
        sample=sample[np.isfinite(sample)]
        if len(sample)>=10:
            volatility[i]=float(np.mean(sample))
    training=volatility[:max(1,int(len(volatility)*.60))]
    training=training[np.isfinite(training)]
    if len(training)<30:
        return [None for _ in tick_indices]
    low,high=np.quantile(training,[1/3,2/3])
    output=[]
    for index in tick_indices:
        value=volatility[int(index)]
        output.append(None if not np.isfinite(value) else "LOW" if value<=low else "MID" if value<=high else "HIGH")
    return output

def _block_bootstrap_ci(values, seed=417, block_length=50, repetitions=2000):
    arr=np.asarray(values,dtype=float); n=len(arr)
    if n==0: return None
    if n==1: return [float(arr[0]),float(arr[0])]
    rng=np.random.default_rng(seed); length=min(block_length,n); nblocks=math.ceil(n/length)
    means=np.empty(repetitions,dtype=float)
    for j in range(repetitions):
        starts=rng.integers(0,n-length+1,size=nblocks)
        sample=np.concatenate([arr[a:a+length] for a in starts])[:n]
        means[j]=sample.mean()
    return [float(np.quantile(means,.025)),float(np.quantile(means,.975))]

def _ev_stats(rows):
    usable=[r for r in rows if r.get("payout") not in (None,"") and r.get("stake") not in (None,"") and r.get("probabilities") and r.get("actual") is not None]
    if not usable: return {"n_with_actual_quote":0,"mean_realized_profit_per_contract":None,"realized_profit_ci95":None,"mean_model_expected_value":None,"model_expected_value_ci95":None,"mean_break_even_hit_rate":None,"observed_hit_rate_minus_mean_break_even":None,"hit_rate_minus_break_even_ci95":None,"quote_coverage":0.0,"bootstrap_block_length":50}
    realized=[]; model_ev=[]; be=[]; hit_minus_be=[]
    for r in usable:
        prob=float(r["probabilities"][int(r["prediction"])])
        payout=float(r["payout"]); stake=float(r["stake"]); hit=int(r["hit"])
        realized.append((payout-stake) if hit else -stake)
        model_ev.append(prob*payout-stake)
        be_rate=stake/payout; be.append(be_rate); hit_minus_be.append(hit-be_rate)
    return {"n_with_actual_quote":len(realized),"mean_realized_profit_per_contract":float(np.mean(realized)),
            "realized_profit_ci95":_block_bootstrap_ci(realized),"mean_model_expected_value":float(np.mean(model_ev)),
            "model_expected_value_ci95":_block_bootstrap_ci(model_ev,seed=418),"mean_break_even_hit_rate":float(np.mean(be)),
            "observed_hit_rate_minus_mean_break_even":float(np.mean(hit_minus_be)),
            "hit_rate_minus_break_even_ci95":_block_bootstrap_ci(hit_minus_be,seed=422),
            "quote_coverage":len(usable)/max(1,len(rows)),"bootstrap_block_length":min(50,len(realized))}

def _summary(group, quote_map):
    n=len(group); k=int(group.hit.sum()) if n else 0; ci=wilson(k,n)
    p=binomtest(k,n,.1,alternative="greater").pvalue if n else 1.0
    probs=list(group.probabilities) if n else []; actuals=list(group.actual.astype(int)) if n else []
    brier=float(np.mean([np.sum((np.asarray(pr)-np.eye(10)[a])**2) for pr,a in zip(probs,actuals)])) if n else None
    predicted_probs=[float(np.asarray(r["probabilities"],dtype=float)[int(r["prediction"])]) for r in group.to_dict("records")] if n else []
    mean_predicted_probability=float(np.mean(predicted_probs)) if predicted_probs else None
    rows=[]
    for r in group.to_dict("records"):
        q=quote_map.get((r["strategy_id"],r["version"],int(r["forecast_tick_id"])))
        rows.append({**r,**(q or {})})
    ev=_ev_stats(rows)
    hit_block_ci=_block_bootstrap_ci(group.hit.astype(int).tolist(),seed=419) if n else None
    return {"n":n,"hits":k,"hit_rate":k/n if n else None,"hit_rate_ci95":list(ci) if n else None,"hit_rate_block_bootstrap_ci95":hit_block_ci,
            "p_vs_uniform_10pct_one_sided":float(p),"brier_multiclass_10class":brier,
            "mean_predicted_probability":mean_predicted_probability,
            "absolute_calibration_error":abs(mean_predicted_probability-k/n) if n and mean_predicted_probability is not None else None,
            "reliability":reliability_table(probs,actuals),"payout_adjusted":ev}

def _digit_evidence(frame, quote_map, selected_strategy_id, validation_finalists, test_frame, test_raw, n_ticks):
    if not selected_strategy_id:
        return {"selected_strategy_id":None,"health_definition":"Evidence quality index; never a win probability.","digits":[]}
    strategy=frame[frame.strategy_id==selected_strategy_id]
    validation=strategy[strategy.split=="validation"]
    final_test=test_frame[test_frame.strategy_id==selected_strategy_id]
    final_opened=selected_strategy_id in validation_finalists
    fold_edges=np.linspace(0,int(n_ticks*.80),5,dtype=int)
    output=[]
    validation_summaries=[]
    final_summaries=[]
    for digit in range(10):
        val_group=validation[validation.prediction==digit]
        test_group=final_test[final_test.prediction==digit] if final_opened else final_test.iloc[0:0]
        val_summary=_summary(val_group,quote_map)
        test_summary=_summary(test_group,quote_map) if final_opened else None
        validation_summaries.append(val_summary)
        final_summaries.append(test_summary)
    val_adjusted=holm_adjust([x["p_vs_uniform_10pct_one_sided"] for x in validation_summaries])
    test_adjusted=holm_adjust([x["p_vs_uniform_10pct_one_sided"] for x in final_summaries if x is not None])
    test_adjusted_by_digit=dict(zip([i for i,x in enumerate(final_summaries) if x is not None],test_adjusted))

    for digit in range(10):
        val_summary=validation_summaries[digit]
        test_summary=final_summaries[digit]
        val_summary["holm_adjusted_p_across_digits"]=val_adjusted[digit]
        if test_summary is not None:
            test_summary["holm_adjusted_p_across_digits"]=test_adjusted_by_digit[digit]
        history_group=strategy[strategy.prediction==digit]
        recent_start=max(0,int(history_group.tick_index.max())-499) if not history_group.empty else 0
        recent_group=history_group[history_group.tick_index>=recent_start]
        recent_n=len(recent_group)
        recent_hit_rate=float(recent_group.hit.mean()) if recent_n else None
        folds=[]
        for fold_index,(lo,hi) in enumerate(zip(fold_edges[:-1],fold_edges[1:]),start=1):
            fold=history_group[(history_group.tick_index>=lo)&(history_group.tick_index<hi)]
            fold_ci=wilson(int(fold.hit.sum()),len(fold)) if len(fold) else None
            folds.append({"fold":fold_index,"n":len(fold),"hit_rate":float(fold.hit.mean()) if len(fold) else None,
                          "hit_rate_ci95":list(fold_ci) if fold_ci else None,
                          "passed":bool(len(fold)>=30 and fold_ci and fold_ci[0]>.10)})
        regime_rows=[]
        for regime in ("LOW","MID","HIGH"):
            val_regime=val_group[val_group.volatility_regime==regime]
            test_regime=test_group[test_group.volatility_regime==regime]
            val_ci=wilson(int(val_regime.hit.sum()),len(val_regime)) if len(val_regime) else None
            test_ci_regime=wilson(int(test_regime.hit.sum()),len(test_regime)) if len(test_regime) else None
            total=len(val_regime)+len(test_regime)
            total_hits=int(val_regime.hit.sum())+int(test_regime.hit.sum())
            regime_rows.append({"regime":regime,"n":total,"hit_rate":total_hits/total if total else None,
                                "validation":{"n":len(val_regime),"hit_rate_ci95":list(val_ci) if val_ci else None},
                                "final_test":{"n":len(test_regime),"hit_rate_ci95":list(test_ci_regime) if test_ci_regime else None},
                                "passed":bool(len(val_regime)>=50 and val_ci and val_ci[0]>.10
                                              and len(test_regime)>=50 and test_ci_regime and test_ci_regime[0]>.10)})
        val_ev=val_summary["payout_adjusted"]
        test_ev=test_summary["payout_adjusted"] if test_summary else None
        min_quotes=min(val_ev["n_with_actual_quote"],test_ev["n_with_actual_quote"] if test_ev else 0)
        support_score=min(min_quotes/1000,1.0)*15
        val_rate=val_summary["hit_rate"]
        accuracy_score=max(0,min(1,(val_rate-.10)/.05))*15 if val_rate is not None else 0
        recent_score=max(0,min(1,(recent_hit_rate-.10)/.05))*10 if recent_hit_rate is not None and recent_n>=30 else 0
        val_ci=val_summary["hit_rate_block_bootstrap_ci95"]
        test_ci=test_summary["hit_rate_block_bootstrap_ci95"] if test_summary else None
        lower=min(val_ci[0],test_ci[0]) if val_ci and test_ci else None
        confidence_score=max(0,min(1,(lower-.10)/.05))*15 if lower is not None else 0
        calibration_values=[x["absolute_calibration_error"] for x in (val_summary,test_summary) if x and x["absolute_calibration_error"] is not None]
        calibration_error=float(np.mean(calibration_values)) if calibration_values else None
        calibration_score=max(0,1-calibration_error/.10)*15 if calibration_error is not None else 0
        walk_score=sum(x["passed"] for x in folds)/len(folds)*10
        regime_score=sum(x["passed"] for x in regime_rows)/len(regime_rows)*10
        pnl_ok=bool(val_ev.get("realized_profit_ci95") and val_ev["realized_profit_ci95"][0]>0
                    and test_ev and test_ev.get("realized_profit_ci95") and test_ev["realized_profit_ci95"][0]>0
                    and min_quotes>=1000)
        payout_score=10 if pnl_ok else 0
        health=round(support_score+accuracy_score+recent_score+confidence_score+calibration_score+walk_score+regime_score+payout_score)
        gate_reasons=[]
        if selected_strategy_id not in validation_finalists:
            gate_reasons.append("The development-selected strategy did not pass the pre-registered validation gate.")
        if test_summary is None:
            gate_reasons.append("The untouched final test is not opened for this strategy.")
        if val_summary["n"]<1000 or not test_summary or test_summary["n"]<1000:
            gate_reasons.append("Fewer than 1,000 scored predictions for this digit in validation and final test.")
        if min_quotes<1000 or val_ev["quote_coverage"]<.90 or not test_ev or test_ev["quote_coverage"]<.90:
            gate_reasons.append("Insufficient contemporaneous quoted and settled contracts for this digit.")
        if val_adjusted[digit]>=.05 or test_summary and test_summary["holm_adjusted_p_across_digits"]>=.05:
            gate_reasons.append("Digit-specific hit-rate evidence does not pass Holm correction across all ten digits.")
        if not (val_ci and val_ci[0]>.10 and test_ci and test_ci[0]>.10):
            gate_reasons.append("The block-bootstrap hit-rate lower bound is not above the 10% reference in both holdouts.")
        if not pnl_ok:
            gate_reasons.append("Positive quote-backed profit confidence bounds are not established in both holdouts.")
        if not all(x["passed"] for x in regime_rows):
            gate_reasons.append("Performance is not confirmed across all three volatility regimes.")
        output.append({
            "digit":digit,"validation":val_summary,"final_test":test_summary,
            "recent":{"n":recent_n,"hit_rate":recent_hit_rate},
            "walk_forward_folds":folds,"volatility_regimes":regime_rows,
            "health_score":health,
            "health_components":{"support":round(support_score,2),"historical_performance":round(accuracy_score,2),
                                 "recent_performance":round(recent_score,2),"uncertainty":round(confidence_score,2),
                                 "calibration":round(calibration_score,2),"walk_forward_stability":round(walk_score,2),
                                 "volatility_stability":round(regime_score,2),"payout_profitability":payout_score},
            "evidence_qualified":not gate_reasons,"gate_reasons":gate_reasons,
            "decision":"DO NOT TRADE" if gate_reasons else "PAPER TRADE CANDIDATE",
        })
    return {"selected_strategy_id":selected_strategy_id,"selection_basis":"Best multiclass Brier score on development data; no validation or final-test tuning.",
            "health_definition":"0-100 evidence-quality index from scored sample support, historical/recent hit rate, block-bootstrap uncertainty, calibration, walk-forward and volatility stability, and quote-backed profitability. It is not a win probability.",
            "minimum_per_digit_quotes_per_holdout":1000,"digits":output}

def _walk_forward(frame, n_ticks, quote_map, folds=4):
    """Four chronological diagnostic folds over development+validation only; final test is excluded."""
    cutoff=int(n_ticks*.80); edges=np.linspace(0,cutoff,folds+1,dtype=int); output=[]
    eligible=frame[frame.tick_index<cutoff]
    for i in range(folds):
        lo,hi=int(edges[i]),int(edges[i+1]); block=eligible[(eligible.tick_index>=lo)&(eligible.tick_index<hi)]
        strategies={}
        pvals=[]; ids=[]
        for sid,g in block.groupby("strategy_id"):
            summary=_summary(g,quote_map)
            ids.append(sid); pvals.append(summary["p_vs_uniform_10pct_one_sided"]); strategies[sid]=summary
        adj=holm_adjust(pvals)
        for sid,padj in zip(ids,adj): strategies[sid]["holm_adjusted_p"]=padj
        output.append({"fold":i+1,"tick_index_range_exclusive_end":[lo,hi],"strategies":strategies})
    return output

def _verdict(summary, minimum_n=5000, minimum_quoted_n=5000):
    if not summary: return "inconclusive"
    ev=summary.get("payout_adjusted") or {}
    hit_ci=summary.get("hit_rate_block_bootstrap_ci95")
    pnl_ci=ev.get("realized_profit_ci95")
    if summary.get("n",0)<minimum_n or ev.get("n_with_actual_quote",0)<minimum_quoted_n or ev.get("quote_coverage",0)<.90:
        return "inconclusive"
    if hit_ci and hit_ci[1] is not None and hit_ci[1] <= .10:
        return "no_evidence_of_edge"
    if pnl_ci and pnl_ci[1] is not None and pnl_ci[1] < 0:
        return "no_evidence_of_edge"
    if (summary.get("holm_adjusted_p",1)<.05 and hit_ci and hit_ci[0]>.10
            and pnl_ci and pnl_ci[0]>0):
        return "evidence_of_edge"
    return "inconclusive"

def evaluate_symbol(ticks: pd.DataFrame, symbol: str, stored_forecasts=None):
    if ticks.empty or len(ticks)<101:
        return {"symbol":symbol,"n_ticks":len(ticks),"status":"insufficient_data","strategies":[],"final_test_accessed":False}
    frame=_forecast_frame(ticks,symbol)
    if frame.empty: return {"symbol":symbol,"n_ticks":len(ticks),"status":"insufficient_data","strategies":[],"final_test_accessed":False}
    # Only immutable quote events captured against a particular forecast are eligible for EV.
    quote_map={}
    for r in (stored_forecasts or []):
        if r.get("actual_digit") is not None and r.get("payout") and r.get("stake") and r.get("probabilities"):
            quote_map[(r["strategy_id"],r["strategy_version"],int(r["forecast_tick_id"]))]={"payout":r["payout"],"stake":r["stake"],"probabilities":r["probabilities"],"prediction":r["prediction"]}
    dev=frame[frame.split=="development"]; val=frame[frame.split=="validation"]
    dev_rank={}
    dev_summaries={sid:_summary(g,quote_map) for sid,g in dev.groupby("strategy_id")}
    dev_ids=list(dev_summaries); dev_adj=holm_adjust([dev_summaries[s]["p_vs_uniform_10pct_one_sided"] for s in dev_ids])
    for sid,padj in zip(dev_ids,dev_adj): dev_summaries[sid]["holm_adjusted_p"]=padj
    for sid,g in dev.groupby("strategy_id"):
        dev_rank[sid]=dev_summaries[sid]["brier_multiclass_10class"]
    val_raw={sid:_summary(g,quote_map) for sid,g in val.groupby("strategy_id")}
    sids=list(val_raw); adjusted=holm_adjust([val_raw[s]["p_vs_uniform_10pct_one_sided"] for s in sids])
    for sid,padj in zip(sids,adjusted): val_raw[sid]["holm_adjusted_p"]=padj
    gates=PRE_REGISTERED["validation_gates"]; finalists=[]
    for sid in sids:
        v=val_raw[sid]; evci=v["payout_adjusted"]["realized_profit_ci95"]
        if v["n"]>=gates["minimum_n"] and v["payout_adjusted"]["n_with_actual_quote"]>=gates["minimum_quoted_n"] and v["payout_adjusted"]["quote_coverage"]>=gates["minimum_quote_coverage"] and v["holm_adjusted_p"]<gates["Holm_adjusted_one_sided_p_lt"] and v["hit_rate_block_bootstrap_ci95"] and v["hit_rate_block_bootstrap_ci95"][0]>gates["hit_rate_block_CI95_lower_gt"] and evci and evci[0]>gates["realized_profit_CI95_lower_gt"]:
            finalists.append(sid)
    test=frame[(frame.split=="final_test") & frame.strategy_id.isin(finalists)]
    test_raw={sid:_summary(g,quote_map) for sid,g in test.groupby("strategy_id")} if finalists else {}
    if test_raw:
        test_ids=list(test_raw); test_adj=holm_adjust([test_raw[s]["p_vs_uniform_10pct_one_sided"] for s in test_ids])
        for sid,padj in zip(test_ids,test_adj): test_raw[sid]["holm_adjusted_p"]=padj
    result=[]
    for sid in PRE_REGISTERED["candidate_ids"]:
        d=dev[dev.strategy_id==sid]; v=val[val.strategy_id==sid]
        validation_summary=val_raw.get(sid)
        final_summary=test_raw.get(sid)
        validation_verdict=_verdict(validation_summary,gates["minimum_n"],gates["minimum_quoted_n"])
        final_verdict=_verdict(final_summary,gates["minimum_n"],gates["minimum_quoted_n"]) if final_summary else None
        row={"strategy_id":sid,"development":dev_summaries.get(sid),"validation":validation_summary,"validation_verdict":validation_verdict,
             "final_test":final_summary,"final_test_verdict":final_verdict,
             "final_test_status":"evaluated_once_as_validation_finalist" if sid in finalists else "not_opened_not_a_validation_finalist"}
        row["verdict"]=final_verdict if final_verdict else validation_verdict
        result.append(row)
    selected_strategy_id=min(dev_rank,key=lambda sid:dev_rank[sid] if dev_rank[sid] is not None else float("inf"),default=None)
    digit_evidence=_digit_evidence(frame,quote_map,selected_strategy_id,finalists,test,test_raw,len(ticks))
    digit_counts={str(i):int((ticks.digit.astype(int)==i).sum()) for i in range(10)}
    return {"symbol":symbol,"n_ticks":len(ticks),"max_tick_id":int(ticks.id.max()),"baseline_theoretical_hit_rate":0.10,"baseline_null":"uniform independent digit guess; not asserted as market truth","empirical_digit_counts":digit_counts,"empirical_digit_frequencies":{k:v/len(ticks) for k,v in digit_counts.items()},"split_boundaries":{"development_end_tick_index":int(len(ticks)*.60),"validation_end_tick_index":int(len(ticks)*.80)},
            "development_brier_rank":dict(sorted(dev_rank.items(),key=lambda x:x[1] if x[1] is not None else float('inf'))),
            "walk_forward_development_validation_only":_walk_forward(frame,len(ticks),quote_map),
            "selected_strategy_id":selected_strategy_id,"digit_evidence":digit_evidence,
            "validation_finalists":finalists,"final_test_accessed":bool(finalists),"strategies":result,
            "limitations":["A 1-second forecast is scored only against a tick with epoch exactly forecast_epoch+1; missed epochs remain unscored.","Payout-adjusted EV uses only proposal quotes captured after that forecast and before the next tick; missing or late quotes are excluded.","Final test is not evaluated for non-finalists. Final-test results are not used to retune.","The 10% null assumes uniform independent digits; it is a theoretical reference, not a proven property of every symbol.","A validation finalist is not evidence of durable edge until the untouched test and subsequent forward paper test agree."]}

def generate_report(store, symbols):
    import json
    reports=[]
    for symbol in symbols:
        # Once any validation finalist opens the final test, freeze the entire report snapshot.
        # Subsequent API calls must reuse it and may not re-test on newly arriving ticks.
        frozen=store.research_snapshot_versioned(symbol,PROTOCOL_VERSION)
        if frozen:
            item=json.loads(frozen["report_json"])
            item["research_snapshot"]={"frozen":True,"protocol_version":PROTOCOL_VERSION,"created_utc":frozen["created_utc"],"n_ticks":frozen["n_ticks"],"max_tick_id":frozen["max_tick_id"]}
        else:
            frame=store.ticks_frame(symbol)
            item=evaluate_symbol(frame,symbol,store.forecasts(symbol))
            item.setdefault("pre_registration",PRE_REGISTERED)
            if item.get("final_test_accessed"):
                item["research_snapshot"]={"frozen":True,"protocol_version":PROTOCOL_VERSION,"created_utc":None,"n_ticks":len(frame),"max_tick_id":int(frame.id.max()) if not frame.empty else None}
                payload=json.dumps(item,allow_nan=False,default=str)
                store.freeze_research_snapshot_versioned(symbol,PROTOCOL_VERSION,len(frame),int(frame.id.max()) if not frame.empty else None,payload)
                # Re-read to report the canonical first-write snapshot in a concurrent request race.
                frozen=store.research_snapshot_versioned(symbol,PROTOCOL_VERSION)
                if frozen:
                    item=json.loads(frozen["report_json"])
                    item["research_snapshot"]={"frozen":True,"protocol_version":PROTOCOL_VERSION,"created_utc":frozen["created_utc"],"n_ticks":frozen["n_ticks"],"max_tick_id":frozen["max_tick_id"]}
        reports.append(item)
    return {"report_version":"1.0.0","pre_registration":PRE_REGISTERED,"symbols":reports}
