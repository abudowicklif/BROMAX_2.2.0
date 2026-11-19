import asyncio, base64, binascii, hmac, logging, os, time
from contextlib import asynccontextmanager
from copy import deepcopy
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pathlib import Path
import json
from threading import Lock
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from .config import Settings
from .storage import Store
from .collector import MarketCollector
from .paperbot import ExecutionInterface, PaperBot
from .agents import (
    AGENT_ROLES,
    build_combined_candidate,
    coordinate_snapshot,
    select_primary_signal,
)
from .reporting import write_report
from .intelligence import analyze_ticks
from .timing import evaluate_timing, evaluate_payout_timing
from uuid import uuid4

logging.basicConfig(level=logging.INFO)
settings=Settings.from_env(); store=Store(settings.database_path)
paperbot=PaperBot(store,settings.paper_stake,settings.max_paper_stake,settings.daily_loss_cap,settings.max_trades_per_day)
execution_interface=ExecutionInterface()
collector=MarketCollector(settings,store,paperbot)
collector_task=None
primary_analysis_task=None
_report_cache={"created":0.0,"value":None}
_primary_report_file_cache={"path":None,"modified_ns":None,"value":None}
_timing_cache={}
_report_lock=Lock()
_timing_lock=Lock()
_primary_signal_cache={}
_primary_signal_lock=Lock()
_primary_analysis_lock=Lock()
_agent_activity_lock=Lock()
_decision_analysis_lock=Lock()
_PRIMARY_FULL_ANALYSIS_INTERVAL_SECONDS=60.0
_PRIMARY_ANALYSIS_POLL_SECONDS=1.0
_last_primary_full_analysis_at={}
_primary_analysis_state={
    symbol:{
        "status":"WAITING_FOR_DATA",
        "last_started_at":None,
        "last_completed_at":None,
        "last_tick_id":None,
        "last_checked_tick_id":None,
        "last_signal_status":None,
        "last_reason":None,
        "last_error":None,
    }
    for symbol in settings.symbols
}
_agent_activity_state={
    symbol:{
        "cycle_count":0,
        "last_cycle_at":None,
        "last_tick_id":None,
        "cycle_status":"NOT_RUN",
        "complete_agent_count":0,
        "agent_run_persisted":False,
        "combined_candidate":None,
        "coordination":{},
        "agents":{
            agent_id:{
                "agent_id":agent_id,
                "role":role,
                "run_count":0,
                "execution_status":"NOT_RUN",
                "result_status":"NOT_RUN",
                "validation_status":"NOT_EVALUATED" if agent_id=="strategy_research" else "NOT_APPLICABLE",
                "evaluated_at":None,
                "duration_ms":None,
                "summary":"No agent cycle has completed yet.",
                "reasons":[],
                "metrics":{},
                "output":{},
            }
            for agent_id,role in AGENT_ROLES.items()
        },
    }
    for symbol in settings.symbols
}
_PIPELINE_FORECAST_COUNT_TTL_SECONDS=60.0
_pipeline_forecast_count_cache={}
_pipeline_forecast_count_lock=Lock()

def _auth_credentials():
    username=os.getenv("BROMAX_AUTH_USERNAME")
    password=os.getenv("BROMAX_AUTH_PASSWORD")
    if bool(username) != bool(password):
        raise RuntimeError("Set both BROMAX_AUTH_USERNAME and BROMAX_AUTH_PASSWORD")
    return username,password

@asynccontextmanager
async def lifespan(app: FastAPI):
    global collector_task, primary_analysis_task
    auth_required=os.getenv("BROMAX_REQUIRE_AUTH","").lower() in {"1","true","yes"}
    username,password=_auth_credentials()
    if auth_required and not username:
        raise RuntimeError("BROMAX_REQUIRE_AUTH is enabled but authentication credentials are not configured")
    collector_task=asyncio.create_task(collector.run_forever())
    primary_analysis_task=asyncio.create_task(_primary_signal_worker())
    try:
        yield
    finally:
        tasks=[task for task in (primary_analysis_task,collector_task) if task is not None]
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

app=FastAPI(title="BROMAX MATCH Research",version="2.2.0",lifespan=lifespan)

def _market_feed_is_fresh(symbol, now=None):
    received=collector.last_received.get(symbol)
    if received is None:
        return False
    age=(time.time() if now is None else now)-received
    return -1<=age<=settings.max_data_age_seconds

def _set_primary_analysis_state(symbol,status,**updates):
    with _primary_analysis_lock:
        state=_primary_analysis_state.setdefault(symbol,{})
        state.update(status=status,updated_at=datetime.now(timezone.utc).isoformat(),**updates)

def _primary_analysis_snapshot():
    with _primary_analysis_lock:
        return {symbol:dict(state) for symbol,state in _primary_analysis_state.items()}

def _agent_activity_snapshot():
    with _agent_activity_lock:
        return deepcopy(_agent_activity_state)

def _agent_evidence_for_tick(symbol,tick_id):
    activity=_agent_activity_snapshot().get(symbol,{})
    agents=activity.get("agents",{})
    matches_tick=(
        tick_id is not None
        and activity.get("last_tick_id")==tick_id
        and all(
            agents.get(agent_id,{}).get("source_tick_id")==tick_id
            for agent_id in AGENT_ROLES
        )
    )
    return {
        "status":activity.get("cycle_status","NOT_RUN") if matches_tick else "PENDING",
        "snapshot_tick_id":activity.get("last_tick_id"),
        "matches_candidate_tick":bool(matches_tick),
        "complete_agent_count":activity.get("complete_agent_count",0) if matches_tick else 0,
        "required_agent_count":len(AGENT_ROLES),
        "agent_statuses":{
            agent_id:{
                "execution_status":agents[agent_id].get("execution_status"),
                "result_status":agents[agent_id].get("result_status"),
                "validation_status":agents[agent_id].get("validation_status"),
            }
            for agent_id in AGENT_ROLES
        } if matches_tick else {},
    }

def _record_agent_activity(symbol,decision,cycle_duration_ms=None,agent_run_persisted=None):
    results={
        result.get("agent_id"):result
        for result in decision.get("agents",[])
        if isinstance(result,dict) and result.get("agent_id")
    }
    snapshot_tick_id=decision.get("snapshot_tick_id")
    completed=sum(
        agent_id in results and results[agent_id].get("status")!="ERROR"
        for agent_id in AGENT_ROLES
    )
    missing=[agent_id for agent_id in AGENT_ROLES if agent_id not in results]
    has_errors=any(result.get("status")=="ERROR" for result in results.values())
    with _agent_activity_lock:
        state=_agent_activity_state.setdefault(symbol,{
            "cycle_count":0,
            "last_cycle_at":None,
            "last_tick_id":None,
            "cycle_status":"NOT_RUN",
            "complete_agent_count":0,
            "agent_run_persisted":False,
            "combined_candidate":None,
            "coordination":{},
            "agents":{},
        })
        state.update(
            cycle_count=state["cycle_count"]+1,
            last_cycle_at=decision.get("created_at") or datetime.now(timezone.utc).isoformat(),
            last_tick_id=snapshot_tick_id,
            cycle_status="INCOMPLETE" if missing else "COMPLETED_WITH_ERRORS" if has_errors else "COMPLETE",
            complete_agent_count=completed,
            missing_agents=missing,
            agent_run_persisted=bool(agent_run_persisted),
            cycle_duration_ms=cycle_duration_ms,
            final_status=decision.get("final_status"),
            primary_signal_status=(decision.get("primary_signal") or {}).get("status"),
            combined_candidate=deepcopy(decision.get("combined_candidate")),
            coordination=deepcopy(decision.get("coordination") or {}),
        )
        agents=state.setdefault("agents",{})
        for agent_id,role in AGENT_ROLES.items():
            result=results.get(agent_id)
            if result is None:
                previous=agents.get(agent_id,{})
                previous.update(
                    agent_id=agent_id,
                    role=role,
                    execution_status="MISSING",
                    result_status="MISSING",
                    summary="No result was returned by this agent cycle.",
                    reasons=["The coordinator did not receive this agent's result."],
                )
                agents[agent_id]=previous
                continue
            result_status=result.get("status","UNKNOWN")
            if agent_id=="strategy_research":
                validation_status={
                    "APPROVED":"VALIDATED",
                    "INSUFFICIENT_EVIDENCE":"RESEARCH_PENDING",
                    "REJECTED":"NOT_VALIDATED",
                    "ERROR":"ERROR",
                }.get(result_status,"UNKNOWN")
            else:
                validation_status="NOT_APPLICABLE"
            previous=agents.get(agent_id,{})
            previous.update({
                "agent_id":agent_id,
                "role":role,
                "run_count":int(previous.get("run_count",0))+1,
                "execution_status":"ERROR" if result_status=="ERROR" else "EXECUTED",
                "result_status":result_status,
                "validation_status":validation_status,
                "evaluated_at":result.get("evaluated_at"),
                "duration_ms":result.get("duration_ms"),
                "summary":result.get("summary"),
                "reasons":list(result.get("reasons") or []),
                "metrics":deepcopy(result.get("metrics") or {}),
                "output":deepcopy(result.get("output") or {}),
                "source_tick_id":snapshot_tick_id,
            })
            agents[agent_id]=previous

def _primary_failure_reason(primary):
    reasons=[str(reason) for reason in primary.get("reasons") or []]
    return next(
        (reason for reason in reasons if not reason.startswith("Required analysis is not ready")),
        reasons[0] if reasons else None,
    )

def _load_primary_report_item(symbol):
    report_path=Path(__file__).resolve().parent.parent/"reports"/"bromax_report.json"
    with _report_lock:
        report_data=_report_cache["value"]
        report_is_fresh=(
            report_data is not None
            and time.monotonic()-_report_cache["created"]<15
        )
        if not report_is_fresh:
            if not report_path.is_file():
                return None
            modified_ns=report_path.stat().st_mtime_ns
            if (
                _primary_report_file_cache["path"]==str(report_path)
                and _primary_report_file_cache["modified_ns"]==modified_ns
            ):
                report_data=_primary_report_file_cache["value"]
            else:
                report_data=json.loads(report_path.read_text(encoding="utf-8"))
                _primary_report_file_cache.update(
                    path=str(report_path),modified_ns=modified_ns,value=report_data
                )
    return next(
        (item for item in report_data.get("symbols",[]) if item.get("symbol")==symbol),
        {},
    )

def _run_live_agent_cycle(symbol,signal_data,quality_ticks,feed_stale):
    started=time.perf_counter()
    report_item=_load_primary_report_item(symbol) or {}
    evidence=report_item.get("digit_evidence") or {}
    strategy_id=evidence.get("selected_strategy_id") or report_item.get("selected_strategy_id")
    forecasts_by_id={
        str(row.get("strategy_id")):row
        for row in (signal_data.get("signals") or [])
        if isinstance(row,dict) and row.get("strategy_id")
    }
    selected_forecast=forecasts_by_id.get(str(strategy_id)) if strategy_id else None
    probabilities=(selected_forecast or {}).get("probabilities") or []
    evidence_by_digit={}
    for row in evidence.get("digits") or []:
        try:
            evidence_by_digit[int(row["digit"])]=row
        except (KeyError,TypeError,ValueError):
            continue

    recent_ticks=[]
    for row in store.quality_history(symbol,1000):
        try:
            digit=int(row["digit"])
        except (KeyError,TypeError,ValueError):
            continue
        if digit in range(10):
            recent_ticks.append(row)
    market_diagnostics=analyze_ticks(recent_ticks,symbol,1000)
    market_state=market_diagnostics.get("market_state") or {}
    digit_rows=[]
    for digit in range(10):
        evidence_row=evidence_by_digit.get(digit,{})
        validation=evidence_row.get("validation") or {}
        probability=probabilities[digit] if len(probabilities)==10 else None
        digit_rows.append({
            "digit":digit,
            "estimated_win_probability":probability,
            "health_percent":evidence_row.get("health_score"),
            "validation":validation,
            "durations":{
                str(duration):{
                    "decision":"DO NOT TRADE",
                    "current_quote":{"valid":False,"spot_time":None},
                }
                for duration in (1,2,3,4,5)
            },
        })

    current_forecasts=store.forecasts(symbol,limit=20)
    latest_epoch=signal_data.get("observed_epoch")
    current_selected=next(
        (
            row for row in current_forecasts
            if str(row.get("strategy_id"))==str(strategy_id)
            and row.get("forecast_epoch") is not None
            and int(row["forecast_epoch"])==int(latest_epoch)
        ),
        None,
    ) if strategy_id and latest_epoch is not None else None
    ready={
        "status":"RESEARCH_ONLY",
        "eligible":False,
        "reason":"The background cycle checks exploratory quotes; this is not an approved paper signal.",
        "approved_strategy_ids":[],
    }
    if current_selected is not None:
        prediction=int(current_selected["prediction"])
        quote=store.duration_quote(current_selected["seq"],1)
        quote_spot_time=None
        if quote:
            try:
                quote_spot_time=int(quote.get("quote_spot_time"))
            except (TypeError,ValueError):
                quote_spot_time=None
        quote_data={
            "valid":bool(
                quote
                and quote_spot_time==int(current_selected["forecast_epoch"])
                and not feed_stale
            ),
            "spot_time":quote_spot_time,
            "stake":quote.get("stake") if quote else None,
            "payout":quote.get("payout") if quote else None,
        }
        digit_rows[prediction]["durations"]["1"]["current_quote"]=quote_data
        vector=current_selected.get("probabilities") or []
        probability=vector[prediction] if len(vector)==10 else None
        expected_value=None
        if quote and probability is not None:
            try:
                expected_value=str(
                    Decimal(str(probability))*Decimal(str(quote["payout"]))
                    -Decimal(str(quote["stake"]))
                )
            except (InvalidOperation,KeyError,TypeError,ValueError,ArithmeticError):
                expected_value=None
        ready["research_candidate"]={
            "predicted_digit":prediction,
            "duration":1,
            "probability":str(probability) if probability is not None else None,
            "stake":quote.get("stake") if quote else None,
            "payout":quote.get("payout") if quote else None,
            "expected_value":expected_value,
            "forecast_epoch":int(current_selected["forecast_epoch"]),
            "duration_profitability_status":"RESEARCH_ONLY",
        }

    digit_analysis={
        "digits":digit_rows,
        "volatility_filter":{
            "status":"INSUFFICIENT DATA",
            "regime":None,
            "health_percent":None,
            "checks_passed":0,
            "checks_total":0,
        },
        "volatility":{
            "short_term":market_state.get("mean_absolute_return_last_100"),
            "long_term":market_state.get("mean_absolute_return_full_window"),
        },
    }
    timing={
        "strategies":[],
        "current_volatility_regime":None,
        "current_volatility":market_state.get("mean_absolute_return_last_100"),
        "short_term_volatility":market_state.get("mean_absolute_return_last_100"),
        "long_term_volatility":market_state.get("mean_absolute_return_full_window"),
        "exploratory_diagnostics":{
            "sample_size":market_diagnostics.get("sample_size"),
            "digit_counts":market_diagnostics.get("digit_counts"),
            "uniformity_diagnostic":market_diagnostics.get("uniformity_diagnostic"),
            "patterns":market_diagnostics.get("patterns"),
            "transition_matrix_smoothed":market_diagnostics.get("transition_matrix_smoothed"),
        },
    }
    tick_id=signal_data.get("observed_tick_id")
    latest_ticks=store.quality_history(symbol,1)
    try:
        latest_epoch=int(latest_ticks[-1]["epoch"]) if latest_ticks else None
        observed_epoch=int(signal_data["observed_epoch"])
    except (KeyError,TypeError,ValueError):
        latest_epoch=observed_epoch=None
    consistent=bool(
        tick_id is not None
        and latest_epoch is not None
        and observed_epoch is not None
        and store.is_latest_tick(symbol,tick_id)
        and latest_epoch==observed_epoch
    )
    timestamp=datetime.now(timezone.utc).isoformat()
    coordinated=coordinate_snapshot(
        report_item=report_item,
        digit_analysis=digit_analysis,
        signal_data=signal_data,
        timing=timing,
        ready=ready,
        quality_ticks=quality_ticks,
        feed_stale=feed_stale,
        max_data_age_seconds=settings.max_data_age_seconds,
        collector_errors=collector.errors,
        paper_events=store.paper_events(symbol),
        forecasts=store.forecasts(symbol,limit=500),
        prior_runs=store.agent_runs(symbol,limit=20),
        consistent=consistent,
        snapshot_timestamp=timestamp,
    )
    decision={
        "created_at":timestamp,
        "snapshot_tick_id":tick_id,
        "final_status":coordinated["decision"].get("final_status"),
        "agents":coordinated["agents"],
        "coordination":coordinated["coordination"],
        "combined_candidate":coordinated["combined_candidate"],
        "primary_signal":coordinated["primary_signal"],
    }
    decision_key="|".join((
        str(decision["final_status"]),
        str(decision["primary_signal"].get("status")),
        str(decision["primary_signal"].get("strategy_id")),
        str(decision["primary_signal"].get("digit")),
        "|".join(
            f"{item.get('agent_id')}:{item.get('status')}"
            for item in coordinated["agents"]
        ),
    ))
    persisted=store.record_agent_run(
        snapshot_id=str(uuid4()),
        symbol=symbol,
        snapshot_tick_id=tick_id,
        final_status=decision["final_status"] or "NO TRADE — INSUFFICIENT EVIDENCE",
        decision_key=decision_key,
        agent_results=coordinated["agents"],
    )
    _record_agent_activity(
        symbol,
        decision,
        cycle_duration_ms=round((time.perf_counter()-started)*1000,3),
        agent_run_persisted=persisted,
    )
    with _primary_signal_lock:
        _primary_signal_cache[symbol]=coordinated["primary_signal"]
    return coordinated

def _run_primary_analysis_once(symbol):
    tick_id=None
    full_analysis_started=False
    try:
        signal_data=collector.signal_snapshot(symbol)
        if signal_data.get("status") in {"analysis_pending","insufficient_history"}:
            _set_primary_analysis_state(
                symbol,
                "ANALYSIS_PENDING",
                last_tick_id=signal_data.get("observed_tick_id"),
                last_reason="Waiting for a complete strategy forecast batch.",
            )
            return False

        tick_id=signal_data.get("observed_tick_id")
        with _primary_analysis_lock:
            if _primary_analysis_state.get(symbol,{}).get("last_checked_tick_id")==tick_id:
                return False
        _set_primary_analysis_state(
            symbol,
            "ANALYZING",
            last_started_at=datetime.now(timezone.utc).isoformat(),
            last_tick_id=tick_id,
            last_checked_tick_id=tick_id,
            last_reason=None,
            last_error=None,
        )
        quality_ticks=store.quality_history(symbol,21)
        feed_stale=not _market_feed_is_fresh(symbol)
        coordinated=_run_live_agent_cycle(
            symbol,signal_data,quality_ticks,feed_stale
        )
        primary=coordinated["primary_signal"]
        statuses={
            result.get("agent_id"):result.get("status")
            for result in coordinated["agents"]
        }
        full_analysis_eligible=all(
            statuses.get(agent_id)=="APPROVED"
            for agent_id in ("data_intelligence","strategy_research","probability_calibration")
        )
        if full_analysis_eligible:
            with _primary_analysis_lock:
                last_full_analysis=_last_primary_full_analysis_at.get(symbol)
                now_monotonic=time.monotonic()
                full_analysis_throttled=(
                    last_full_analysis is not None
                    and now_monotonic-last_full_analysis<_PRIMARY_FULL_ANALYSIS_INTERVAL_SECONDS
                )
                if not full_analysis_throttled:
                    _last_primary_full_analysis_at[symbol]=now_monotonic
            if not full_analysis_throttled:
                full_analysis_started=True
                decision=unified_decision(symbol)
                primary=decision["primary_signal"]
                with _primary_analysis_lock:
                    _last_primary_full_analysis_at[symbol]=time.monotonic()

        current=collector.signal_snapshot(symbol)
        still_current=(
            current.get("status") not in {"analysis_pending","insufficient_history"}
            and not feed_stale
            and primary.get("observed_tick_id")==current.get("observed_tick_id")
            and primary.get("observed_epoch")==current.get("observed_epoch")
        )
        if feed_stale:
            status="STALE_RESULT"
            reason="The last market tick is stale; agent findings are not a current signal."
        elif not still_current:
            status="STALE_RESULT"
            reason="The live tick changed during agent analysis; result is not current."
        else:
            status="QUALIFIED" if primary["status"]=="QUALIFIED" else "NO_QUALIFIED_SIGNAL"
            reason=_primary_failure_reason(primary)
        _set_primary_analysis_state(
            symbol,
            status,
            last_completed_at=datetime.now(timezone.utc).isoformat(),
            last_tick_id=primary.get("observed_tick_id"),
            last_checked_tick_id=tick_id,
            last_signal_status=primary.get("status"),
            last_reason=reason,
            last_error=None,
        )
        return True
    except Exception as error:
        logging.getLogger("bromax.primary").exception(
            "Background primary analysis failed for %s",symbol
        )
        if full_analysis_started:
            with _primary_analysis_lock:
                _last_primary_full_analysis_at[symbol]=time.monotonic()
        _set_primary_analysis_state(
            symbol,
            "ERROR",
            last_completed_at=datetime.now(timezone.utc).isoformat(),
            last_tick_id=tick_id,
            last_checked_tick_id=tick_id,
            last_reason="Background primary analysis failed.",
            last_error=str(error),
        )
        return True

async def _primary_signal_worker():
    while True:
        for symbol in settings.symbols:
            try:
                await asyncio.to_thread(_run_primary_analysis_once,symbol)
            except Exception as error:
                logging.getLogger("bromax.primary").exception(
                    "Background primary worker failed for %s",symbol
                )
                _set_primary_analysis_state(
                    symbol,
                    "ERROR",
                    last_completed_at=datetime.now(timezone.utc).isoformat(),
                    last_reason="Background primary worker failed.",
                    last_error=str(error),
                )
        await asyncio.sleep(_PRIMARY_ANALYSIS_POLL_SECONDS)

@app.middleware("http")
async def require_basic_auth(request, call_next):
    username,password=_auth_credentials()
    auth_required=os.getenv("BROMAX_REQUIRE_AUTH","").lower() in {"1","true","yes"}
    if not username:
        if auth_required:
            raise RuntimeError("BROMAX_REQUIRE_AUTH is enabled but authentication credentials are not configured")
        return await call_next(request)
    header=request.headers.get("authorization","")
    scheme,separator,encoded=header.partition(" ")
    if separator and scheme.lower()=="basic":
        try:
            decoded=base64.b64decode(encoded,validate=True).decode("utf-8")
        except (binascii.Error,UnicodeDecodeError,ValueError):
            decoded=""
        supplied_username,colon,supplied_password=decoded.partition(":")
        if (colon and hmac.compare_digest(supplied_username.encode("utf-8"),username.encode("utf-8"))
                and hmac.compare_digest(supplied_password.encode("utf-8"),password.encode("utf-8"))):
            return await call_next(request)
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="BROMAX", charset="UTF-8"'},
    )

@app.get("/health")
def health():
    with store.connect() as c:
        counts={s:c.execute("SELECT COUNT(*) FROM ticks WHERE symbol=?",(s,)).fetchone()[0] for s in settings.symbols}
        latest={}
        for s in settings.symbols:
            r=c.execute("SELECT epoch,quote_text,received_utc FROM ticks WHERE symbol=? ORDER BY epoch DESC,id DESC LIMIT 1",(s,)).fetchone()
            latest[s]=dict(r) if r else None
    stale={s:not _market_feed_is_fresh(s) for s in settings.symbols}
    pipeline={s:_pipeline_state(s,stored_ticks=counts[s]) for s in settings.symbols}
    return {"status":"degraded" if any(stale.values()) else "ok","mode":"paper_only","trading_enabled":False,"symbols":settings.symbols,"tick_counts":counts,"latest":latest,"stale":stale,"pipeline":pipeline,"primary_analysis":_primary_analysis_snapshot(),"agent_activity":_agent_activity_snapshot(),"recent_errors":collector.errors[-10:]}

@app.get("/performance")
def performance():
    return {
        "symbols":{
            symbol:{
                "connection_status":collector.connection_status,
                "pipeline":collector.operational_status(symbol),
            }
            for symbol in settings.symbols
        },
        "timing_source":"local monotonic clock; tick-to-forecast excludes Deriv network transit",
    }

def _stored_forecast_count(symbol):
    now=time.monotonic()
    with _pipeline_forecast_count_lock:
        cached=_pipeline_forecast_count_cache.get(symbol)
        if cached and now-cached[0] < _PIPELINE_FORECAST_COUNT_TTL_SECONDS:
            return cached[1]
        with store.connect() as c:
            count=c.execute(
                "SELECT COUNT(*) FROM audit_events WHERE symbol=? AND event_type='FORECAST'",(symbol,)
            ).fetchone()[0]
        _pipeline_forecast_count_cache[symbol]=(now,count)
        return count

def _pipeline_state(symbol,stored_ticks=None):
    state=collector.operational_status(symbol)
    state["recent_errors"]=collector.errors[-5:]
    if stored_ticks is None:
        with store.connect() as c:
            stored_ticks=c.execute("SELECT COUNT(*) FROM ticks WHERE symbol=?",(symbol,)).fetchone()[0]
    state["stored_ticks"]=stored_ticks
    state["stored_forecasts"]=_stored_forecast_count(symbol)
    return state

@app.post("/safety/kill-switch")
def engage_execution_kill_switch():
    """Engage the one-way live-execution kill switch; never enables orders."""
    execution_interface.engage_kill_switch()
    logging.getLogger("bromax.safety").warning("Execution kill switch engaged")
    return {
        "status":"KILL_SWITCH_ENGAGED",
        "kill_switch_engaged":execution_interface.kill_switch_engaged,
        "live_execution_implemented":False,
        "live_execution_enabled":False,
    }

@app.get("/ticks/latest")
def latest_ticks(symbol: str, limit: int=50):
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    with store.connect() as c:
        rows=c.execute("SELECT id,epoch,quote_text,pip_size,digit,received_utc FROM ticks WHERE symbol=? ORDER BY epoch DESC,id DESC LIMIT ?",(symbol,max(1,min(limit,1000)))).fetchall()
    return [dict(r) for r in rows]

@app.get("/digits/distribution")
def distribution(symbol: str, window: int=1000):
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    rows=store.history(symbol,max(10,min(window,10000))); counts={str(i):0 for i in range(10)}
    for r in rows: counts[str(r["digit"])] += 1
    n=len(rows)
    return {"symbol":symbol,"n":n,"counts":counts,"frequencies":{k:(v/n if n else None) for k,v in counts.items()},"uniform_reference":0.1}

@app.get("/forecasts")
def forecasts(symbol: str, limit: int = 500):
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    if limit < 1 or limit > 5000: raise HTTPException(422,"limit must be between 1 and 5000")
    return store.forecasts(symbol,limit=limit)

@app.get("/signals/history")
def signal_history(symbol: str, limit: int = 50):
    if symbol not in settings.symbols:
        raise HTTPException(404,"Symbol not configured")
    if limit < 1 or limit > 500:
        raise HTTPException(422,"limit must be between 1 and 500")
    rows=store.recent_forecasts(symbol,limit=limit)
    latest=store.quality_history(symbol,1)
    latest_epoch=int(latest[-1]["epoch"]) if latest else None
    status=_pipeline_state(symbol)
    now=time.time()
    current_feed=(
        collector.last_received.get(symbol) is not None
        and now-collector.last_received[symbol]<=settings.max_data_age_seconds
    )
    signals=[]
    for row in rows:
        actual=row.get("actual_digit")
        if actual is not None:
            signal_status="SCORED_HIT" if row.get("hit") else "SCORED_MISS"
        elif current_feed and int(row["forecast_epoch"])==latest_epoch:
            signal_status="CURRENT"
        else:
            signal_status="EXPIRED_UNSCORED"
        probabilities=row.get("probabilities") or []
        prediction=int(row["prediction"])
        confidence=probabilities[prediction] if prediction<len(probabilities) else None
        signals.append({
            "symbol":symbol,
            "strategy_id":row["strategy_id"],
            "prediction":prediction,
            "confidence":confidence,
            "forecast_epoch":int(row["forecast_epoch"]),
            "target_epoch":int(row["forecast_epoch"])+1,
            "created_at":row["created_utc"],
            "status":signal_status,
            "actual_digit":actual,
            "hit":row.get("hit"),
        })
    return {"symbol":symbol,"pipeline":status,"signals":signals,"trade_authorized":False,
            "note":"Persisted strategy forecasts are analysis records, not trade instructions. CURRENT applies only to the freshest tick while the live feed is fresh."}

@app.get("/report")
def report():
    with _report_lock:
        if _report_cache["value"] is None or time.monotonic()-_report_cache["created"]>=15:
            _report_cache["value"]=write_report(store,settings.symbols)
            _report_cache["created"]=time.monotonic()
        return _report_cache["value"]

@app.get("/bridge/status")
def bridge_status():
    """Health of the intelligence-to-paper-execution handoff; never places live orders."""
    report_path=Path("reports/bromax_report.json")
    approvals_path=Path("approved_strategies.json")
    approvals={}
    if approvals_path.exists():
        try: approvals=json.loads(approvals_path.read_text()).get("approved",{})
        except Exception: approvals={}
    latest={}
    for symbol in settings.symbols:
        rows=store.forecasts(symbol,limit=1)
        row=rows[-1] if rows else None
        latest[symbol]={
            "forecast": row,
            "handoff": "paper_candidate_only" if row else "waiting_for_forecast",
            "approved_strategy_ids": approvals.get(symbol,[]),
        }
    payout_choices={symbol:evaluate_payout_timing(store.duration_events(symbol)) for symbol in settings.symbols}
    return {
        "system": "BROMAX Intelligence + Deriv Paper Execution Bridge",
        "mode": "SIMULATED_ONLY",
        "market_data": "public_deriv_ticks",
        "quote_source": "Deriv_read_only_proposals",
        "execution": "paper_only_no_buy_endpoint",
        "strategy_approval_file_present": approvals_path.exists(),
        "research_report_present": report_path.exists(),
        "symbols": latest,
        "contract_protocol": {"type": "DIGITMATCH", "duration_candidates": [1,2,3,4,5], "duration_unit": "t", "selection": "quote_backed_out_of_sample_profitability"},
        "payout_optimized_duration": {s:r.get("best_duration") for s,r in payout_choices.items()},
        "note": "The intelligence engine freezes forecasts, captures read-only Deriv proposals for 1-5 ticks, and settles duration research contracts against exact future ticks. The READY panel exposes only the statistically selected duration after quote, volatility and risk checks. No real account authentication or live order placement exists."
    }


@app.get("/signals/ready")
def ready_signal(symbol: str):
    """Expose the statistically selected duration when its live quote is still valid.

    The collector has already recorded every candidate duration in the paper
    quote/settlement ledger. This endpoint never buys or sends a live order.
    """
    from decimal import Decimal, InvalidOperation
    import time as _time
    if symbol not in settings.symbols:
        raise HTTPException(404, "Symbol not configured")
    rows=store.forecasts(symbol)
    approved_ids=[]
    try:
        approval_data=json.loads(Path("approved_strategies.json").read_text())
        approved_ids=approval_data.get("approved",{}).get(symbol,[])
    except Exception: pass
    if not rows:
        return {"symbol":symbol,"status":"WAITING","eligible":False,"reason":"No forecast has been recorded for this market yet.","signal":None,"approved_strategy_ids":approved_ids}
    payout_evidence=evaluate_payout_timing(store.duration_events(symbol))
    best=payout_evidence.get("best_candidate")
    if not best:
        return {"symbol":symbol,"status":"WAITING","eligible":False,
                "reason":"No duration has passed quote-backed, chronological, payout-adjusted out-of-sample profitability testing. Candidate forecasts remain research-only.",
                "signal":None,"approved_strategy_ids":approved_ids,
                "payout_timing":{"status":payout_evidence.get("status"),"best_duration":None}}
    strategy_id=best.get("strategy_id"); duration=int(best.get("duration_ticks") or 0)
    if strategy_id not in approved_ids or not paperbot.approved(symbol,strategy_id):
        return {"symbol":symbol,"status":"WAITING","eligible":False,
                "reason":"The payout-optimized strategy does not have the separate strategy approval required for paper signals.",
                "signal":None,"approved_strategy_ids":approved_ids,
                "payout_timing":{"best_duration":duration,"strategy_id":strategy_id}}
    latest=next((row for row in reversed(rows) if row.get("strategy_id")==strategy_id),None)
    if latest is None or latest.get("hit") is not None:
        return {"symbol":symbol,"status":"WAITING","eligible":False,
                "reason":"Waiting for a fresh forecast from the payout-optimized strategy.","signal":None,
                "approved_strategy_ids":approved_ids,"payout_timing":{"best_duration":duration}}
    q=store.duration_quote(latest["seq"],duration)
    if not q:
        return {"symbol":symbol,"status":"WAITING","eligible":False,
                "reason":"The selected duration does not have a contemporaneous Deriv proposal quote for this forecast tick.",
                "signal":None,"approved_strategy_ids":approved_ids,"payout_timing":{"best_duration":duration}}
    if int(q.get("quote_spot_time") or 0)!=int(latest.get("forecast_epoch") or 0) or _time.time()>=int(latest["forecast_epoch"])+1:
        return {"symbol":symbol,"status":"EXPIRED","eligible":False,
                "reason":"The duration-specific proposal quote is stale or its one-tick entry window has expired.",
                "signal":None,"approved_strategy_ids":approved_ids}
    try:
        probability=Decimal(str((latest.get("probabilities") or [])[int(latest["prediction"])]))
        payout=Decimal(str(q["payout"])); stake=Decimal(str(q["stake"]))
        expected_value=probability*payout-stake; break_even=stake/payout
    except (InvalidOperation,ValueError,TypeError,IndexError,ZeroDivisionError):
        return {"symbol":symbol,"status":"REJECTED","eligible":False,"reason":"Forecast probabilities or duration proposal values failed validation.","signal":None,"approved_strategy_ids":approved_ids}
    if expected_value<=0:
        return {"symbol":symbol,"status":"REJECTED","eligible":False,
                "reason":"The current duration-specific proposal does not have positive model-estimated expected value.","signal":None,
                "approved_strategy_ids":approved_ids}
    timing_evidence=evaluate_timing(store.history(symbol,limit=10000),rows)
    current_regime=timing_evidence.get("current_volatility_regime")
    timing_row=next((x for x in timing_evidence.get("strategies",[]) if x.get("strategy_id")==strategy_id and x.get("duration_ticks")==duration),None)
    regime_row=next((x for x in (timing_row or {}).get("volatility_regimes",[]) if x.get("regime")==current_regime),None)
    if not regime_row or regime_row.get("status")!="CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON":
        return {"symbol":symbol,"status":"REJECTED","eligible":False,
                "reason":"Volatility confirmation is not established for the selected strategy, duration and current regime on untouched final-test data.",
                "signal":None,"approved_strategy_ids":approved_ids,
                "volatility":{"regime":current_regime,"status":"UNCONFIRMED"}}
    if not paperbot.limits_ok(symbol,stake):
        return {"symbol":symbol,"status":"REJECTED","eligible":False,
                "reason":"Paper stake, daily loss or trade-count limits block this opportunity.","signal":None,"approved_strategy_ids":approved_ids}
    signal={"signal_id":f"BROMAX-{latest['seq']}-T{duration}","symbol":symbol,
            "strategy_id":strategy_id,"predicted_digit":int(latest["prediction"]),
            "contract":"DIGITMATCH","duration":duration,"duration_unit":"t",
            "stake":str(stake),"payout":str(payout),"probability":str(probability),
            "break_even_probability":str(break_even),"expected_value":str(expected_value),
            "volatility_regime":current_regime,"volatility_confirmation":"CONFIRMED",
            "duration_profitability_status":"OUT_OF_SAMPLE_PROFIT_EVIDENCE",
            "final_test_mean_return":best["final_test"].get("mean_return_on_stake"),
            "final_test_return_ci95":best["final_test"].get("mean_return_ci95"),
            "expires_epoch":int(latest["forecast_epoch"])+1,"forecast_epoch":int(latest["forecast_epoch"]),
            "proposal_id":q.get("proposal_id"),"execution_mode":"PAPER_SIMULATION_ONLY"}
    return {"symbol":symbol,"status":"READY","eligible":True,
            "reason":"The best quote-backed duration passed final-test profitability, current quote EV, strategy approval, volatility confirmation and paper-risk checks. It is tracked in the duration paper ledger; no live order is placed.",
            "signal":signal,"approved_strategy_ids":approved_ids}


@app.get("/paper/status")
def paper_status():
    events=store.paper_events()
    opened=[e for e in events if e["event_type"]=="PAPER_OPEN"]
    settled=[e for e in events if e["event_type"]=="PAPER_SETTLE"]
    settled_ids={e["note"] for e in settled}
    pending=sum(1 for e in opened if str(e["seq"]) not in settled_ids)
    return {"enabled":True,"mode":"SIMULATED_ONLY","stake":str(settings.paper_stake),"max_stake":str(settings.max_paper_stake),"daily_loss_cap":str(settings.daily_loss_cap),"max_trades_per_day":settings.max_trades_per_day,"opened_total":len(opened),"settled_total":len(settled),"pending":pending,"approved_strategies_file":"approved_strategies.json","live_execution":False}


@app.get("/paper/trades")
def paper_trades(symbol: str | None = None):
    if symbol is not None and symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    return store.paper_events(symbol)


@app.get("/intelligence")
def intelligence(symbol: str, window: int = 1000):
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    if window < 100 or window > 10000: raise HTTPException(422,"window must be between 100 and 10000")
    frame=store.ticks_frame(symbol)
    rows=frame.tail(window).to_dict(orient="records") if not frame.empty else []
    return analyze_ticks(rows,symbol,window)

@app.get("/timing")
def timing_research(symbol: str):
    """Compare 1-5 tick horizons and volatility regimes; research only."""
    if symbol not in settings.symbols:
        raise HTTPException(404, "Symbol not configured")
    with _timing_lock:
        cached=_timing_cache.get(symbol)
        if cached and time.monotonic()-cached["created"]<5:
            return cached["value"]
        ticks = store.history(symbol, limit=10000)
        forecasts = store.forecasts(symbol, limit=50000)
        result = evaluate_timing(ticks, forecasts)
        result["payout_optimization"] = evaluate_payout_timing(store.duration_events(symbol))
        result["symbol"] = symbol
        _timing_cache[symbol]={"created":time.monotonic(),"value":result}
        return result

def _digit_duration_results(events, strategy_id):
    quotes={}
    settled={}
    for row in events:
        key=(int(row.get("forecast_seq") or 0),int(row.get("duration_ticks") or 0))
        if row.get("event_type")=="QUOTE":
            quotes[key]=row
        elif row.get("event_type")=="SETTLE":
            settled[key]=row
    result={(digit,duration):{"n":0,"wins":0,"net_pnl":0.0,"stake":0.0} for digit in range(10) for duration in (1,2,3,4,5)}
    for key,quote in quotes.items():
        outcome=settled.get(key)
        if not outcome or quote.get("strategy_id")!=strategy_id:
            continue
        try:
            epoch=int(quote["forecast_epoch"])
            duration=int(quote["duration_ticks"])
            prediction=int(quote["prediction"])
            if int(quote["quote_spot_time"])!=epoch or int(outcome["actual_epoch"])!=epoch+duration:
                continue
            stake=Decimal(str(quote["stake"])); payout=Decimal(str(quote["payout"]))
            hit=int(outcome["hit"])
        except (KeyError,TypeError,ValueError,ArithmeticError):
            continue
        if prediction not in range(10) or duration not in range(1,6) or stake<=0:
            continue
        row=result[(prediction,duration)]
        row["n"]+=1
        row["wins"]+=hit
        row["net_pnl"]+=float(payout-stake if hit else -stake)
        row["stake"]+=float(stake)
    for row in result.values():
        row["hit_rate"]=row["wins"]/row["n"] if row["n"] else None
        row["mean_return_on_stake"]=row["net_pnl"]/row["stake"] if row["stake"] else None
        row["net_pnl"]=round(row["net_pnl"],8)
    return result

@app.get("/digits/analysis")
def digit_analysis(symbol: str):
    if symbol not in settings.symbols:
        raise HTTPException(404,"Symbol not configured")
    report_data=report()
    report_item=next((x for x in report_data.get("symbols",[]) if x.get("symbol")==symbol),{})
    evidence=report_item.get("digit_evidence") or {"selected_strategy_id":None,"digits":[]}
    strategy_id=evidence.get("selected_strategy_id")
    signal_data=signals(symbol)
    current_model=next((x for x in signal_data.get("signals",[]) if x.get("strategy_id")==strategy_id),None)
    rows_by_digit={int(x["digit"]):x for x in evidence.get("digits",[])}
    history=store.history(symbol,limit=1000)
    recent=history[-100:] if history else []
    counts={digit:sum(int(row["digit"])==digit for row in history) for digit in range(10)}
    recent_counts={digit:sum(int(row["digit"])==digit for row in recent) for digit in range(10)}
    latest_quotes=store.forecasts(symbol,limit=10)
    latest_epoch=max((int(x["forecast_epoch"]) for x in latest_quotes),default=None)
    latest_quotes=[x for x in latest_quotes if latest_epoch is not None and int(x["forecast_epoch"])==latest_epoch]
    duration_events=store.duration_events(symbol)
    duration_stats=_digit_duration_results(duration_events,strategy_id)
    approved=bool(strategy_id and paperbot.approved(symbol,strategy_id))
    timing=timing_research(symbol)
    payout=timing.get("payout_optimization") or {}
    best_duration=payout.get("best_candidate")
    timing_rows={(x.get("strategy_id"),int(x.get("duration_ticks") or 0)):x for x in payout.get("strategies",[])}
    volatility_rows={(x.get("strategy_id"),int(x.get("duration_ticks") or 0)):x for x in timing.get("strategies",[])}
    current_regime=timing.get("current_volatility_regime")
    current_volatility_row=volatility_rows.get((strategy_id,1),{})
    current_regime_evidence=next((x for x in current_volatility_row.get("volatility_regimes",[])
                                  if x.get("regime")==current_regime),None)
    if current_regime_evidence:
        val=current_regime_evidence.get("validation") or {}
        final=current_regime_evidence.get("final_test") or {}
        volatility_checks=[
            val.get("n",0)>=1000,
            bool(val.get("hit_rate_ci95") and val["hit_rate_ci95"][0]>.10),
            val.get("holm_adjusted_p",1)<.05,
            final.get("n",0)>=1000,
            bool(final.get("hit_rate_ci95") and final["hit_rate_ci95"][0]>.10),
            final.get("holm_adjusted_p",1)<.05,
        ]
        volatility_filter={
            "regime":current_regime,
            "status":"PASS" if current_regime_evidence.get("status")=="CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON" else "FAIL",
            "health_percent":round(100*sum(volatility_checks)/len(volatility_checks)),
            "checks_passed":sum(volatility_checks),
            "checks_total":len(volatility_checks),
        }
    else:
        volatility_filter={"regime":current_regime,"status":"INSUFFICIENT DATA","health_percent":None,
                           "checks_passed":0,"checks_total":6}
    digit_output=[]
    now=time.time()
    for digit in range(10):
        evidence_row=rows_by_digit.get(digit,{})
        validation=evidence_row.get("validation") or {}
        current_probability=None
        if current_model and digit<len(current_model.get("probabilities",[])):
            current_probability=float(current_model["probabilities"][digit])
        quote_by_duration={}
        for duration in (1,2,3,4,5):
            quote=None
            quote_forecast=None
            for forecast in latest_quotes:
                if int(forecast["prediction"])!=digit:
                    continue
                candidate=store.duration_quote(forecast["seq"],duration)
                if candidate:
                    quote=candidate
                    quote_forecast=forecast
                    break
            valid=bool(quote and quote_forecast
                       and int(quote.get("quote_spot_time") or 0)==int(quote_forecast["forecast_epoch"])
                       and now<int(quote_forecast["forecast_epoch"])+1)
            probability=current_probability
            ev=break_even=None
            if valid and probability is not None:
                try:
                    stake=Decimal(str(quote["stake"])); payout_value=Decimal(str(quote["payout"]))
                    ev=str(Decimal(str(probability))*payout_value-stake)
                    break_even=str(stake/payout_value) if payout_value else None
                except (InvalidOperation,KeyError,TypeError,ValueError,ZeroDivisionError):
                    valid=False
            duration_history=duration_stats[(digit,duration)]
            historical=timing_rows.get((strategy_id,duration),{})
            vol_candidate=volatility_rows.get((strategy_id,duration),{})
            current_regime=timing.get("current_volatility_regime")
            volatility_confirmed=any(x.get("regime")==current_regime and x.get("status")=="CONFIRMED_FOR_THIS_STRATEGY_AND_HORIZON"
                                     for x in vol_candidate.get("volatility_regimes",[]))
            duration_gate=bool(best_duration
                                and best_duration.get("strategy_id")==strategy_id
                                and int(best_duration.get("duration_ticks") or 0)==duration
                                and historical.get("payout_ev_status")=="OUT_OF_SAMPLE_PROFIT_EVIDENCE")
            reasons=list(evidence_row.get("gate_reasons") or [])
            if not approved:
                reasons.append("This strategy has not passed the separate strategy approval file check.")
            if not current_model or int(current_model.get("predicted_digit",-1))!=digit:
                reasons.append("The development-selected live forecast does not predict this digit.")
            if not valid:
                reasons.append("No contemporaneous unexpired Deriv proposal quote is available for this digit and duration.")
            if ev is None or Decimal(ev)<=0:
                reasons.append("The current model probability does not show positive expected value at this quote.")
            if duration!=1:
                reasons.append("Per-digit untouched-test evidence has not been frozen for this duration.")
            if not duration_gate:
                reasons.append("This strategy-duration pair has no qualifying quote-backed final-test result.")
            if not volatility_confirmed:
                reasons.append("Volatility compatibility is not confirmed for this strategy and duration.")
            limits_ok=bool(valid and quote and paperbot.limits_ok(symbol,Decimal(str(quote["stake"]))))
            if not limits_ok:
                reasons.append("Current paper risk limits do not allow this stake.")
            qualified=bool(evidence_row.get("evidence_qualified") and approved and current_model
                           and int(current_model.get("predicted_digit",-1))==digit
                           and valid and ev is not None and Decimal(ev)>0 and duration==1
                           and duration_gate and volatility_confirmed and limits_ok)
            quote_by_duration[str(duration)]={
                "historical":{"n":duration_history["n"],"hit_rate":duration_history["hit_rate"],
                              "net_pnl":duration_history["net_pnl"],"mean_return_on_stake":duration_history["mean_return_on_stake"],
                              "out_of_sample_status":historical.get("payout_ev_status","RESEARCH_ONLY")},
                "current_quote":{"available":bool(quote),"valid":valid,
                                 "stake":quote.get("stake") if quote else None,
                                 "payout":quote.get("payout") if quote else None,
                                 "break_even_probability":break_even,"expected_value":ev,
                                 "spot_time":quote.get("quote_spot_time") if quote else None},
                "decision":"PAPER TRADE CANDIDATE" if qualified else "DO NOT TRADE",
                "reasons":[] if qualified else list(dict.fromkeys(reasons)),
            }
        digit_output.append({
            "digit":digit,"estimated_win_probability":current_probability,
            "historical_observed_match_rate":validation.get("hit_rate"),
            "confidence_interval_95":validation.get("hit_rate_ci95"),
            "health_percent":evidence_row.get("health_score") if evidence_row else None,
            "health_components":evidence_row.get("health_components",{}),
            "sample_size":validation.get("n",0),
            "quoted_sample_size":validation.get("payout_adjusted",{}).get("n_with_actual_quote",0),
            "current_frequency":counts[digit]/len(history) if history else None,
            "recent_frequency":recent_counts[digit]/len(recent) if recent else None,
            "validation":validation,"final_test":evidence_row.get("final_test"),
            "recent_performance":evidence_row.get("recent"),
            "walk_forward_folds":evidence_row.get("walk_forward_folds",[]),
            "volatility_regimes":evidence_row.get("volatility_regimes",[]),
            "durations":quote_by_duration,
            "decision":"PAPER TRADE CANDIDATE" if any(x["decision"]=="PAPER TRADE CANDIDATE" for x in quote_by_duration.values()) else "DO NOT TRADE",
            "reasons":list(dict.fromkeys(
                list(evidence_row.get("gate_reasons") or [])
                +[reason for duration_data in quote_by_duration.values() for reason in duration_data["reasons"]]
            )),
        })
    settled=[x for x in store.paper_events(symbol) if x.get("event_type")=="PAPER_SETTLE"]
    def performance_groups(key):
        groups={}
        for row in settled:
            name=str(row.get(key) if row.get(key) is not None else "unknown")
            value=groups.setdefault(name,{"contracts":0,"wins":0,"net_pnl":0.0})
            value["contracts"]+=1
            value["wins"]+=int(row.get("hit") or 0)
            value["net_pnl"]+=float(row.get("paper_pnl") or 0)
        for value in groups.values():
            value["win_rate"]=value["wins"]/value["contracts"] if value["contracts"] else None
            value["net_pnl"]=round(value["net_pnl"],8)
        return groups
    tick_stale=bool(collector.last_received.get(symbol) is None or now-collector.last_received[symbol]>settings.max_data_age_seconds)
    market_health=0 if tick_stale or len(history)<2 else round(100*sum(int(b["epoch"])-int(a["epoch"])==1 for a,b in zip(history[:-1],history[1:]))/(len(history)-1))
    strategies_by_id={x.get("strategy_id"):x for x in report_item.get("strategies",[])}
    strategy_output=[]
    for candidate in signal_data.get("signals",[]):
        result=strategies_by_id.get(candidate["strategy_id"],{})
        validation=result.get("validation") or {}
        strategy_output.append({
            "strategy_id":candidate["strategy_id"],"version":candidate["version"],
            "predicted_digit":candidate["predicted_digit"],
            "predicted_digit_probability":candidate["predicted_digit_probability"],
            "probabilities":candidate["probabilities"],
            "validation_sample_size":validation.get("n"),
            "validation_hit_rate":validation.get("hit_rate"),
            "validation_brier":validation.get("brier_multiclass_10class"),
            "validation_calibration":validation.get("reliability",[]),
            "validation_status":result.get("validation_verdict","INSUFFICIENT_DATA"),
            "final_test_status":result.get("final_test_verdict") or result.get("final_test_status"),
            "selected":candidate["strategy_id"]==strategy_id,
            "explicitly_approved":paperbot.approved(symbol,candidate["strategy_id"]),
        })
    return {
        "symbol":symbol,"mode":"PAPER_ONLY","trade_authorized":False,
        "evaluated_at":time.time(),"selected_strategy_id":strategy_id,
        "selection_basis":evidence.get("selection_basis"),
        "health_definition":evidence.get("health_definition"),
        "market_health_percent":market_health,"market_feed_stale":tick_stale,
        "latest_tick":dict(history[-1]) if history else None,
        "volatility":{"regime":timing.get("current_volatility_regime"),
                      "reading":timing.get("current_volatility"),
                      "short_term":timing.get("short_term_volatility"),
                      "long_term":timing.get("long_term_volatility")},
        "volatility_filter":volatility_filter,
        "best_duration_candidate":best_duration,
        "strategies":strategy_output,"digits":digit_output,
        "paper_performance":{"by_digit":performance_groups("prediction"),
                             "by_strategy":performance_groups("strategy_id"),
                             "settled_contracts":len(settled)},
        "note":"A digit's health is a research evidence score, not a win probability. Every displayed estimate comes from the current model or recorded outcomes. TRADE status is paper-only and requires all evidence, quote, timing, approval and risk gates.",
    }

@app.get("/decision")
def unified_decision(symbol: str):
    """Return one timestamped decision without overlapping background analysis."""
    if not _decision_analysis_lock.acquire(blocking=False):
        raise HTTPException(503,"Decision analysis is already running; retry after it completes.")
    try:
        decision=_unified_decision_snapshot(symbol)
        _record_agent_activity(
            symbol,
            decision,
            agent_run_persisted=decision.get("agent_run_persisted"),
        )
        return decision
    finally:
        _decision_analysis_lock.release()

def _unified_decision_snapshot(symbol: str):
    """Return one timestamped, internally checked paper-only decision snapshot."""
    from datetime import datetime, timezone
    if symbol not in settings.symbols:
        raise HTTPException(404,"Symbol not configured")
    snapshot_id=str(uuid4())
    snapshot_timestamp=datetime.now(timezone.utc).isoformat()
    initial_ticks=store.quality_history(symbol,1)
    initial_tick_id=initial_ticks[-1]["id"] if initial_ticks else None
    report_data=report()
    report_item=next((item for item in report_data.get("symbols",[]) if item.get("symbol")==symbol),{})
    digit_data=digit_analysis(symbol)
    signal_data=signals(symbol)
    timing_data=timing_research(symbol)
    ready=ready_signal(symbol)
    quality_ticks=store.quality_history(symbol,21)
    paper_events=store.paper_events(symbol)
    forecast_rows=store.forecasts(symbol,limit=5000)
    prior_runs=store.agent_runs(symbol,limit=20)
    final_tick=store.quality_history(symbol,1)
    final_tick_id=final_tick[-1]["id"] if final_tick else None
    current_signal_tick_id=signal_data.get("observed_tick_id")
    digit_tick=(digit_data.get("latest_tick") or {}).get("id")
    consistent=bool(
        initial_tick_id is not None
        and initial_tick_id==final_tick_id
        and current_signal_tick_id==initial_tick_id
        and digit_tick==initial_tick_id
    )
    feed_stale=(
        collector.last_received.get(symbol) is None
        or time.time()-collector.last_received[symbol]>settings.max_data_age_seconds
    )
    coordinated=coordinate_snapshot(
        report_item=report_item,
        digit_analysis=digit_data,
        signal_data=signal_data,
        timing=timing_data,
        ready=ready,
        quality_ticks=quality_ticks,
        feed_stale=feed_stale,
        max_data_age_seconds=settings.max_data_age_seconds,
        collector_errors=collector.errors,
        paper_events=paper_events,
        forecasts=forecast_rows,
        prior_runs=prior_runs,
        consistent=consistent,
        snapshot_timestamp=snapshot_timestamp,
    )
    with _primary_signal_lock:
        _primary_signal_cache[symbol]=coordinated["primary_signal"]
    decision_data=coordinated["decision"]
    risk_result=next(item for item in coordinated["agents"] if item["agent_id"]=="risk_signal_approval")
    approved_signal=decision_data.get("signal") if decision_data.get("trade_authorized") else None
    decision_key="|".join(str(value) for value in (
        decision_data.get("final_status"),
        (approved_signal or {}).get("signal_id"),
        (approved_signal or {}).get("predicted_digit"),
        (approved_signal or {}).get("duration"),
        (approved_signal or {}).get("probability"),
    ))
    persisted=store.record_agent_run(
        snapshot_id=snapshot_id,
        symbol=symbol,
        snapshot_tick_id=final_tick_id,
        final_status=decision_data.get("final_status","NO TRADE — INSUFFICIENT EVIDENCE"),
        decision_key=decision_key,
        agent_results=coordinated["agents"],
    )
    signal_digit=int(approved_signal["predicted_digit"]) if approved_signal else None
    digit_predictions=[]
    for row in digit_data.get("digits",[]):
        is_approved=bool(
            approved_signal
            and int(row["digit"])==signal_digit
            and row.get("decision")=="PAPER TRADE CANDIDATE"
        )
        rejection_reasons=list(row.get("reasons") or [])
        if not is_approved and row.get("decision")=="PAPER TRADE CANDIDATE":
            rejection_reasons.extend(risk_result["reasons"])
        digit_predictions.append({
            "digit":int(row["digit"]),
            "estimated_win_probability":row.get("estimated_win_probability"),
            "health_percent":row.get("health_percent"),
            "decision":"TRADE" if is_approved else "NO TRADE",
            "reasons":[] if is_approved else list(dict.fromkeys(rejection_reasons or ["No fully qualified paper signal for this digit."])),
        })
    data_agent=next(item for item in coordinated["agents"] if item["agent_id"]=="data_intelligence")
    regime_agent=next(item for item in coordinated["agents"] if item["agent_id"]=="market_regime")
    pipeline=_pipeline_state(symbol)
    return {
        "snapshot_id":snapshot_id,
        "created_at":snapshot_timestamp,
        "symbol":symbol,
        "currency":settings.currency,
        "mode":"PAPER_ONLY",
        "snapshot_consistent":consistent,
        "snapshot_tick_id":final_tick_id,
        "latest_tick":final_tick[-1] if final_tick else None,
        "final_status":decision_data.get("final_status"),
        "trade_authorized":bool(decision_data.get("trade_authorized")),
        "signal":approved_signal,
        "reasons":risk_result["reasons"],
        "market_status":{
            "connected":pipeline["connection_status"]=="CONNECTED",
            "connection_status":pipeline["connection_status"],
            "subscription_status":pipeline["subscription_status"],
            "data_status":pipeline["data_status"],
            "pipeline_status":pipeline["pipeline_status"],
            "receiving_data":pipeline["data_status"]=="RECEIVING DATA",
            "data_quality_status":data_agent["status"],
            "freshness_seconds":data_agent["metrics"].get("latest_tick_age_seconds"),
            "latest_tick":final_tick[-1] if final_tick else None,
        },
        "pipeline":pipeline,
        "digits":digit_predictions,
        "volatility":{
            "condition":regime_agent["metrics"].get("current_regime"),
            "health_percent":regime_agent["metrics"].get("volatility_health_percent"),
            "status":regime_agent["status"],
        },
        "agents":coordinated["agents"],
        "performance":coordinated["performance"],
        "coordination":coordinated["coordination"],
        "combined_candidate":coordinated["combined_candidate"],
        "primary_signal":coordinated["primary_signal"],
        "agent_run_persisted":persisted,
    }

@app.get("/signals")
def signals(symbol: str):
    """Current candidate forecasts for inspection only; never a trade authorization."""
    if symbol not in settings.symbols: raise HTTPException(404,"Symbol not configured")
    try:
        snapshot=collector.signal_snapshot(symbol)
    except KeyError:
        raise HTTPException(404,"Symbol not configured")
    candidate=build_combined_candidate(
        snapshot,
        feed_fresh=_market_feed_is_fresh(symbol),
        agent_evidence=_agent_evidence_for_tick(symbol,snapshot.get("observed_tick_id")),
    )
    latest=collector.signal_snapshot(symbol)
    if (
        snapshot.get("observed_tick_id")!=latest.get("observed_tick_id")
        or snapshot.get("observed_epoch")!=latest.get("observed_epoch")
    ):
        candidate=build_combined_candidate(
            snapshot,
            feed_fresh=_market_feed_is_fresh(symbol),
            snapshot_consistent=False,
            agent_evidence=_agent_evidence_for_tick(symbol,snapshot.get("observed_tick_id")),
        )
    return {**snapshot,"combined_candidate":candidate}

@app.get("/signals/primary")
def primary_signal(symbol: str):
    if symbol not in settings.symbols:
        raise HTTPException(404,"Symbol not configured")
    current=collector.signal_snapshot(symbol)
    feed_fresh=_market_feed_is_fresh(symbol)
    with _primary_signal_lock:
        cached=_primary_signal_cache.get(symbol)
    matches_latest=(
        cached is not None
        and current.get("status") != "analysis_pending"
        and cached.get("observed_tick_id")==current.get("observed_tick_id")
        and cached.get("observed_epoch")==current.get("observed_epoch")
    )
    if feed_fresh and matches_latest:
        return cached
    if not feed_fresh:
        reason_status="STALE_MARKET_DATA"
    elif current.get("status")=="analysis_pending":
        reason_status="ANALYSIS_PENDING"
    else:
        reason_status="AGENT_ANALYSIS_NOT_AVAILABLE"
    return select_primary_signal(
        current,
        [],
        {
            "status":reason_status,
            "selection_ready":False,
            "snapshot_tick_id":current.get("observed_tick_id"),
            "snapshot_epoch":current.get("observed_epoch"),
        },
    )

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    options="".join(f'<option value="{s}">{s}</option>' for s in settings.symbols)
    page=(Path(__file__).parent / "dashboard_minimal.html").read_text(encoding="utf-8")
    return page.replace("__SYMBOL_OPTIONS__",options)

@app.get("/advanced", response_class=HTMLResponse)
def advanced_dashboard():
    options="".join(f'<option value="{s}">{s}</option>' for s in settings.symbols)
    page=(Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    return (page.replace("__SYMBOL_OPTIONS__",options)
            .replace("__SYMBOLS_JSON__",json.dumps(list(settings.symbols)))
            .replace("</header>",'<a href="/dashboard" style="color:#9ab9c8;margin-left:12px">Decision terminal</a></header>'))
