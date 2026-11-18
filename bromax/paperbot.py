from datetime import datetime, timezone
import time
from decimal import Decimal
import json
from pathlib import Path

class ExecutionInterface:
    """Deliberately disabled interface. No buy/sell methods exist in BROMAX."""
    enabled = False
    async def execute(self, *args, **kwargs):
        raise RuntimeError("Live execution is intentionally not implemented or enabled")

class PaperBot:
    def __init__(self, store, stake: Decimal, max_stake: Decimal, daily_loss_cap: Decimal, max_trades: int, approvals_path="approved_strategies.json"):
        self.store=store; self.stake=stake; self.max_stake=max_stake; self.daily_loss_cap=daily_loss_cap; self.max_trades=max_trades
        self.approvals_path=Path(approvals_path)
    def approved(self, symbol, strategy_id):
        # Two-key gate: explicit user approval plus machine-checked validation and untouched-test evidence.
        if not self.approvals_path.exists(): return False
        try: approvals=json.loads(self.approvals_path.read_text())
        except Exception: return False
        if strategy_id not in approvals.get("approved",{}).get(symbol,[]): return False
        report_path=Path("reports/bromax_report.json")
        if not report_path.exists(): return False
        try: report=json.loads(report_path.read_text())
        except Exception: return False
        for item in report.get("symbols",[]):
            if item.get("symbol") != symbol or strategy_id not in item.get("validation_finalists",[]): continue
            snapshot=item.get("research_snapshot",{})
            if not snapshot.get("frozen") or snapshot.get("protocol_version") != "1.1.0": return False
            result=next((x for x in item.get("strategies",[]) if x.get("strategy_id")==strategy_id),None)
            if not result or not result.get("final_test"): return False
            test=result["final_test"]; ci=test.get("payout_adjusted",{}).get("realized_profit_ci95")
            ev=test.get("payout_adjusted",{})
            hit_ci=test.get("hit_rate_block_bootstrap_ci95")
            return bool(result.get("final_test_verdict")=="evidence_of_edge" and test.get("n",0)>=5000 and ev.get("n_with_actual_quote",0)>=5000 and ev.get("quote_coverage",0)>=.90 and ci and ci[0]>0 and hit_ci and hit_ci[0]>.10 and test.get("holm_adjusted_p",1)<.05)
        return False
    def limits_ok(self, symbol, next_stake=None):
        today=datetime.now(timezone.utc).date().isoformat()
        next_stake=Decimal(next_stake if next_stake is not None else self.stake)
        if next_stake > self.max_stake: return False
        with self.store.connect() as c:
            settled_rows=c.execute("SELECT paper_pnl FROM audit_events WHERE event_type='PAPER_SETTLE' AND substr(created_utc,1,10)=?",(today,)).fetchall()
            open_rows=c.execute("SELECT o.stake FROM audit_events o WHERE o.event_type='PAPER_OPEN' AND NOT EXISTS (SELECT 1 FROM audit_events z WHERE z.event_type='PAPER_SETTLE' AND z.note=CAST(o.seq AS TEXT))").fetchall()
        open_count=len(open_rows)
        open_risk=sum((Decimal(x[0]) for x in open_rows),Decimal("0"))
        realized=sum((Decimal(x[0]) for x in settled_rows if x[0] is not None),Decimal("0"))
        # Reserve the full stake of every open paper contract as worst-case loss.
        return (len(settled_rows)+open_count < self.max_trades and realized-open_risk-next_stake > -self.daily_loss_cap)
    def record(self, symbol, forecast, forecast_seq, tick_id, epoch, stake, payout, proposal_id, spot_time):
        if not self.approved(symbol,forecast.strategy_id): return "strategy_not_approved"
        if payout is None: return "missing_live_proposal"
        if spot_time is None or int(spot_time) != int(epoch): return "proposal_spot_time_mismatch"
        if stake > self.max_stake: return "quoted_stake_exceeds_max"
        probability=Decimal(str(forecast.probabilities[forecast.prediction]))
        if probability*Decimal(payout)-Decimal(stake) <= 0: return "current_quote_model_ev_nonpositive"
        # Require quote-backed, untouched out-of-sample profitability for the
        # exact strategy and the one-tick duration supported by this executor.
        from .timing import evaluate_payout_timing
        payout_report=evaluate_payout_timing(self.store.duration_events(symbol))
        best=payout_report.get("best_candidate")
        if not best or best.get("strategy_id") != forecast.strategy_id or best.get("duration_ticks") != 1:
            return "duration_specific_payout_edge_not_confirmed_for_one_tick"
        # Apply the same fail-closed volatility confirmation at the actual
        # paper-entry boundary, not only in the read-only READY endpoint.
        from .timing import evaluate_timing
        timing_report=evaluate_timing(self.store.history(symbol,limit=10000),self.store.forecasts(symbol,limit=50000))
        regime=timing_report.get("current_volatility_regime")
        timing_row=next((x for x in timing_report.get("strategies",[]) if x.get("strategy_id")==forecast.strategy_id and x.get("duration_ticks")==1),None)
        regime_row=next((x for x in (timing_row or {}).get("volatility_regimes",[]) if x.get("regime")==regime),None)
        if not regime_row or regime_row.get("status")!="CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON":
            return "volatility_confirmation_not_established"
        if not self.limits_ok(symbol,stake): return "paper_limits_blocked"
        if time.time() >= epoch + 1: return "quote_arrived_after_next_tick_deadline"
        if not self.store.is_latest_tick(symbol,tick_id): return "quote_arrived_after_next_tick"
        # Paper-only open; Store appends settlement on the immediately following tick.
        self.store.append_paper_open(symbol,forecast,forecast_seq,tick_id,epoch,stake,payout,proposal_id,spot_time)
        return "paper_trade_opened"
