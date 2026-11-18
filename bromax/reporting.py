import json
from pathlib import Path
from .research import generate_report, _block_bootstrap_ci

def _paper_summary(events):
    settled=[e for e in events if e["event_type"]=="PAPER_SETTLE"]
    opened=[e for e in events if e["event_type"]=="PAPER_OPEN"]
    settled_open_ids={e["note"] for e in settled}
    open_count=sum(1 for e in opened if str(e["seq"]) not in settled_open_ids)
    pnls=[float(e["paper_pnl"]) for e in settled if e.get("paper_pnl") is not None]
    wins=sum(int(e["hit"] or 0) for e in settled)
    return {
        "status":"descriptive_only_not_proof",
        "opened_contracts":len(opened),"settled_contracts":len(settled),"still_open":open_count,
        "wins":wins,"losses":len(settled)-wins,
        "hit_rate":wins/len(settled) if settled else None,
        "realized_profit_total":sum(pnls) if pnls else 0.0,
        "mean_realized_profit_per_contract":sum(pnls)/len(pnls) if pnls else None,
        "realized_profit_ci95":_block_bootstrap_ci(pnls,seed=421) if pnls else None,
    }

def write_report(store, symbols, path="reports/bromax_report.json"):
    report=generate_report(store,symbols)
    for item in report["symbols"]:
        events=store.paper_events(item["symbol"])
        item["paper_forward_test"]=_paper_summary(events)
        strategy_ids=sorted({e["strategy_id"] for e in events})
        by_strategy=[]
        for strategy_id in strategy_ids:
            by_strategy.append({"strategy_id":strategy_id,**_paper_summary([e for e in events if e["strategy_id"]==strategy_id])})
        item["paper_forward_test_by_strategy"]=by_strategy
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(report,indent=2,default=str))
    return report
