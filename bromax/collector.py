import asyncio, json, logging, math, statistics, time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock
import websockets
from .digits import extract_last_digit, quote_precision, QuotePrecisionError
from .strategies import candidate_strategies, ForecastContext
from .proposals import ProposalClient, ProposalUnavailable
from .paperbot import PaperBot

log=logging.getLogger("bromax.collector")

class MarketCollector:
    HISTORY_LIMIT=1000
    TIMING_SAMPLE_LIMIT=256

    def __init__(self, settings, store, paperbot=None):
        self.settings=settings; self.store=store; self.paperbot=paperbot; self.last_received={}; self.errors=[]
        self.proposals=ProposalClient(settings.ws_url,settings.currency,settings.proposal_timeout_seconds)
        self.proposal_semaphore=asyncio.Semaphore(3)
        self.quote_persistence_lock=asyncio.Lock()
        self.quote_tasks=set()
        self.cumulative_counts={symbol:list(store.digit_counts(symbol)) for symbol in settings.symbols}
        self.strategies=candidate_strategies()
        self._snapshot_lock=Lock()
        self._recent_history={
            symbol:deque((dict(row) for row in store.history(symbol,limit=self.HISTORY_LIMIT)),maxlen=self.HISTORY_LIMIT)
            for symbol in settings.symbols
        }
        self._latest_signal_snapshots={symbol:None for symbol in settings.symbols}
        self._pending_signal_epochs={symbol:None for symbol in settings.symbols}
        for symbol in settings.symbols:
            self._latest_signal_snapshots[symbol]=self._load_persisted_signal_snapshot(symbol)
        self._status_lock=Lock()
        self._timing_samples={symbol:deque(maxlen=self.TIMING_SAMPLE_LIMIT) for symbol in settings.symbols}
        self.connection_status="DISCONNECTED"
        self.connection_started_monotonic=None
        self.reconnect_count=0
        self.connection_error=None
        self._symbol_status={
            symbol:{
                "subscription_status":"NOT_SUBSCRIBED",
                "subscription_count":0,
                "received_ticks":0,
                "processed_ticks":0,
                "duplicate_ticks":0,
                "invalid_ticks":0,
                "out_of_order_ticks":0,
                "missing_tick_count":0,
                "history_seeded_ticks":0,
                "analysis_status":"WAITING_FOR_DATA",
                "analysis_runs":0,
                "strategy_status":"WAITING_FOR_DATA",
                "strategy_runs":0,
                "signal_generation_status":"WAITING_FOR_DATA",
                "signals_generated":0,
                "last_tick_epoch":None,
                "last_tick_id":None,
                "last_analysis_at":None,
                "last_signal_at":None,
                "last_signal_epoch":None,
                "last_error":None,
                "processing_errors":0,
            }
            for symbol in settings.symbols
        }

    def _record_error(self, message, symbol=None):
        with self._status_lock:
            self.errors.append(str(message))
            if len(self.errors)>100: del self.errors[:-100]
            if symbol in self._symbol_status:
                status=self._symbol_status[symbol]
                status["last_error"]=str(message)
                status["processing_errors"]+=1

    def _update_symbol_status(self, symbol, **values):
        with self._status_lock:
            self._symbol_status[symbol].update(values)

    def _increment_symbol_status(self, symbol, key, amount=1):
        with self._status_lock:
            status=self._symbol_status[symbol]
            status[key]+=amount

    def _set_connection_status(self, status, error=None, reconnect=False):
        now=datetime.now(timezone.utc).isoformat()
        with self._status_lock:
            self.connection_status=status
            self.connection_error=error
            if reconnect:
                self.reconnect_count+=1
            for symbol_status in self._symbol_status.values():
                symbol_status["connection_status"]=status
                if status in {"CONNECTING","RECONNECTING","DISCONNECTED","ERROR"}:
                    symbol_status["subscription_status"]="NOT_SUBSCRIBED"
                if status=="CONNECTED":
                    symbol_status["connected_at"]=now
                    symbol_status["last_error"]=None
                elif error:
                    symbol_status["last_error"]=error

    def operational_status(self, symbol):
        if symbol not in self._symbol_status:
            raise KeyError(symbol)
        with self._status_lock:
            result=dict(self._symbol_status[symbol])
            result["connection_status"]=self.connection_status
            result["reconnect_count"]=self.reconnect_count
            result["connection_error"]=self.connection_error
        received=self.last_received.get(symbol)
        age=max(0.0,time.time()-received) if received is not None else None
        result["freshness_seconds"]=round(age,3) if age is not None else None
        result["data_status"]=(
            "NO DATA" if age is None else
            "STALE" if age>self.settings.max_data_age_seconds else
            "RECEIVING DATA"
        )
        if result["connection_status"]=="RECONNECTING":
            result["pipeline_status"]="RECONNECTING"
        elif result.get("last_error"):
            result["pipeline_status"]="ERROR"
        elif result["data_status"]=="STALE":
            result["pipeline_status"]="STALE DATA"
        elif result["subscription_status"]!="SUBSCRIBED":
            result["pipeline_status"]=result["connection_status"]
        elif result["signal_generation_status"]=="GENERATING SIGNALS":
            result["pipeline_status"]="GENERATING SIGNALS"
        elif result["analysis_status"]=="ANALYZING":
            result["pipeline_status"]="ANALYZING"
        else:
            result["pipeline_status"]="RECEIVING DATA"
        result["performance"]=self.performance_metrics(symbol)
        return result

    def performance_metrics(self, symbol):
        if symbol not in self._timing_samples:
            raise KeyError(symbol)
        with self._status_lock:
            samples=list(self._timing_samples[symbol])
        stages=("message_parse_ms","data_processing_ms","strategy_evaluation_ms",
                "signal_persistence_ms","collector_processing_ms","tick_to_forecast_ms")
        metrics={}
        for stage in stages:
            values=sorted(sample[stage] for sample in samples if sample.get(stage) is not None)
            if not values:
                metrics[stage]={"sample_count":0}
                continue
            def percentile(fraction):
                return values[min(len(values)-1,math.ceil(fraction*len(values))-1)]
            metrics[stage]={
                "sample_count":len(values),
                "mean":round(statistics.fmean(values),3),
                "median":round(statistics.median(values),3),
                "p95":round(percentile(.95),3),
                "p99":round(percentile(.99),3),
                "max":round(values[-1],3),
            }
        return {"window_size":self.TIMING_SAMPLE_LIMIT,"stages_ms":metrics}

    def _record_timing_sample(self, symbol, sample):
        with self._status_lock:
            self._timing_samples[symbol].append(sample)

    def _refresh_recent_history(self, symbol):
        history=deque(
            (dict(row) for row in self.store.history(symbol,limit=self.HISTORY_LIMIT)),
            maxlen=self.HISTORY_LIMIT,
        )
        with self._snapshot_lock:
            self._recent_history[symbol]=history

    def _build_signal_snapshot(self, symbol, tick_id, epoch, forecasts, history_support):
        output=[]
        for forecast in forecasts:
            probabilities=list(forecast["probabilities"])
            prediction=int(forecast["prediction"])
            if len(probabilities)!=10 or prediction not in range(10):
                return None
            output.append({
                "strategy_id":forecast["strategy_id"],
                "version":forecast["version"],
                "parameters":dict(forecast["parameters"]),
                "predicted_digit":prediction,
                "predicted_digit_probability":probabilities[prediction],
                "probabilities":probabilities,
                "history_support":history_support,
                "status":"RESEARCH_ONLY_NOT_APPROVED",
            })
        expected_ids={strategy.strategy_id for strategy in self.strategies}
        if len(output)!=len(expected_ids) or {row["strategy_id"] for row in output}!=expected_ids:
            return None
        output.sort(key=lambda row:row["predicted_digit_probability"],reverse=True)
        return {
            "symbol":symbol,
            "observed_tick_id":int(tick_id),
            "observed_epoch":int(epoch),
            "signals":output,
            "trade_authorized":False,
            "note":"Ranking is descriptive only. It is not a calibrated confidence score and does not bypass validation, quote, or paper-risk gates.",
        }

    def _load_persisted_signal_snapshot(self, symbol):
        history=self._recent_history[symbol]
        if len(history)<self.settings.min_history_ticks:
            return None
        latest=history[-1]
        rows=self.store.recent_forecasts(symbol,limit=len(self.strategies))
        expected_ids={strategy.strategy_id for strategy in self.strategies}
        if (len(rows)!=len(expected_ids)
                or {row["strategy_id"] for row in rows}!=expected_ids
                or any(row.get("probabilities") is None
                       or len(row["probabilities"])!=10
                       or row.get("prediction") is None
                       or int(row["prediction"]) not in range(10)
                       or not isinstance(row.get("parameters"),dict)
                       for row in rows)
                or any(int(row["forecast_tick_id"])!=int(latest["id"])
                       or int(row["forecast_epoch"])!=int(latest["epoch"])
                       for row in rows)):
            return None
        return self._build_signal_snapshot(
            symbol,latest["id"],latest["epoch"],rows,len(history)
        )

    def signal_snapshot(self, symbol):
        if symbol not in self._recent_history:
            raise KeyError(symbol)
        with self._snapshot_lock:
            history=list(self._recent_history[symbol])
            pending_epoch=self._pending_signal_epochs[symbol]
            snapshot=self._latest_signal_snapshots[symbol]
        if len(history)<self.settings.min_history_ticks:
            return {
                "symbol":symbol,
                "status":"insufficient_history",
                "required_ticks":self.settings.min_history_ticks,
                "observed_ticks":len(history),
                "signals":[],
                "trade_authorized":False,
            }
        latest=history[-1]
        if (pending_epoch is not None or snapshot is None
                or int(snapshot["observed_tick_id"])!=int(latest["id"])
                or int(snapshot["observed_epoch"])!=int(latest["epoch"])):
            return {
                "symbol":symbol,
                "status":"analysis_pending",
                "observed_tick_id":int(latest["id"]),
                "observed_epoch":int(latest["epoch"]),
                "signals":[],
                "trade_authorized":False,
            }
        result=dict(snapshot)
        result["signals"]=[
            {**row,"parameters":dict(row["parameters"]),"probabilities":list(row["probabilities"])}
            for row in snapshot["signals"]
        ]
        return result

    def _seed_history(self, symbol, history, pip_size):
        """Persist a Deriv ticks_history response without generating retrospective forecasts."""
        prices=history.get("prices") or []
        times=history.get("times") or []
        precision=quote_precision(pip_size)
        if len(prices)!=len(times):
            raise ValueError(f"Historical seed arrays have mismatched lengths for {symbol}")
        inserted=0
        ordered=sorted(zip(times,prices),key=lambda item:int(item[0]))
        for epoch,price in ordered:
            epoch=int(epoch)
            quote_text,digit=extract_last_digit(price,precision)
            if digit is not None and self.store.add_tick(symbol,epoch,quote_text,precision,digit) is not None:
                inserted += 1
        self._refresh_recent_history(symbol)
        self._increment_symbol_status(symbol,"history_seeded_ticks",inserted)
        return inserted
    async def _handle_tick(self, tick, symbol_hint=None, received_at_ns=None, parse_ms=None):
        processing_started_ns=time.perf_counter_ns()
        if received_at_ns is None:
            received_at_ns=processing_started_ns
        symbol=tick.get("symbol") or symbol_hint
        if symbol not in self.settings.symbols: raise ValueError(f"Unexpected symbol {symbol!r}")
        self._increment_symbol_status(symbol,"received_ticks")
        self._update_symbol_status(symbol,subscription_status="SUBSCRIBED")
        try:
            if "quote" not in tick or "epoch" not in tick or "pip_size" not in tick:
                raise ValueError("Tick missing quote/epoch/pip_size; refusing to infer")
            epoch=int(tick["epoch"])
            precision=quote_precision(tick["pip_size"])
            quote_text,digit=extract_last_digit(tick["quote"],precision)
            if digit is None: raise ValueError(f"Symbol {symbol} has no displayed decimal digit for MATCH contracts")
        except (QuotePrecisionError,ValueError,TypeError):
            self._increment_symbol_status(symbol,"invalid_ticks")
            raise
        data_processing_started_ns=time.perf_counter_ns()
        with self._snapshot_lock:
            history=self._recent_history[symbol]
            previous_epoch=int(history[-1]["epoch"]) if history else None
        if previous_epoch is not None and epoch<previous_epoch:
            self._increment_symbol_status(symbol,"out_of_order_ticks")
            return False
        if previous_epoch is not None and epoch==previous_epoch:
            self._increment_symbol_status(symbol,"duplicate_ticks")
            return False
        if previous_epoch is not None and epoch>previous_epoch+1:
            self._increment_symbol_status(symbol,"missing_tick_count",epoch-previous_epoch-1)
        with self._snapshot_lock:
            self._pending_signal_epochs[symbol]=epoch
        try:
            tick_id=self.store.add_tick(symbol,epoch,quote_text,precision,digit,tick.get("id"))
        except Exception:
            with self._snapshot_lock:
                self._pending_signal_epochs[symbol]=None
            raise
        with self._snapshot_lock:
            if tick_id is not None:
                self._recent_history[symbol].append(
                    {"id":tick_id,"epoch":epoch,"digit":digit,"quote_text":quote_text}
                )
            self._pending_signal_epochs[symbol]=None
        if tick_id is None:
            self._increment_symbol_status(symbol,"duplicate_ticks")
            return False
        with self._snapshot_lock:
            history=self._recent_history[symbol]
        self.last_received[symbol]=time.time()
        self._increment_symbol_status(symbol,"processed_ticks")
        self._update_symbol_status(
            symbol,
            last_error=None,
            last_tick_epoch=int(tick["epoch"]),
            last_tick_id=tick_id,
            analysis_status="WAITING_FOR_HISTORY",
            strategy_status="WAITING_FOR_HISTORY",
            signal_generation_status="WAITING_FOR_HISTORY",
        )
        self.cumulative_counts[symbol][digit] += 1
        # First score prior forecasts with this newly arrived tick; then forecast from history ending here.
        self.store.score_open_forecasts(symbol,tick_id,int(tick["epoch"]),digit)
        self.store.settle_duration_contracts(symbol,tick_id,int(tick["epoch"]),digit)
        digits=tuple(int(r["digit"]) for r in history)
        strategy_started_ns=time.perf_counter_ns()
        data_processing_ms=(strategy_started_ns-data_processing_started_ns)/1_000_000
        if len(digits)<self.settings.min_history_ticks:
            completed_ns=time.perf_counter_ns()
            self._record_timing_sample(symbol,{
                "message_parse_ms":parse_ms,
                "data_processing_ms":data_processing_ms,
                "strategy_evaluation_ms":None,
                "signal_persistence_ms":None,
                "collector_processing_ms":(completed_ns-processing_started_ns)/1_000_000,
                "tick_to_forecast_ms":None,
            })
            return True
        latest=history[-1]
        ctx=ForecastContext(symbol,int(latest["id"]),int(latest["epoch"]),digits,tuple(self.cumulative_counts[symbol]))
        self._update_symbol_status(
            symbol,
            analysis_status="ANALYZING",
            strategy_status="ANALYZING",
            signal_generation_status="GENERATING SIGNALS",
        )
        self._increment_symbol_status(symbol,"analysis_runs")
        forecasts=[s.forecast(ctx) for s in self.strategies]
        persistence_started_ns=time.perf_counter_ns()
        strategy_evaluation_ms=(persistence_started_ns-strategy_started_ns)/1_000_000
        # Freeze all forecasts synchronously before returning to the stream reader.
        # Quote requests run in background and are separately timestamped append-only events.
        forecast_seqs=self.store.append_forecasts(
            symbol,forecasts,tick_id,int(tick["epoch"]),None,self.settings.paper_stake,None,"PAYOUT_PENDING"
        )
        forecast_rows=list(zip(forecasts,forecast_seqs))
        signal_snapshot=self._build_signal_snapshot(
            symbol,tick_id,int(tick["epoch"]),
            [{
                "strategy_id":forecast.strategy_id,
                "version":forecast.version,
                "parameters":forecast.parameters,
                "prediction":forecast.prediction,
                "probabilities":forecast.probabilities,
            } for forecast in forecasts],
            len(digits),
        )
        with self._snapshot_lock:
            self._latest_signal_snapshots[symbol]=signal_snapshot
        signal_persistence_ms=(time.perf_counter_ns()-persistence_started_ns)/1_000_000
        generated_at=datetime.now(timezone.utc).isoformat()
        self._update_symbol_status(
            symbol,
            analysis_status="ANALYZED",
            strategy_status="PROCESSED",
            signal_generation_status="GENERATED" if forecasts else "NO SIGNALS",
            last_analysis_at=generated_at,
            last_signal_at=generated_at if forecasts else None,
            last_signal_epoch=int(tick["epoch"]) if forecasts else None,
        )
        self._increment_symbol_status(symbol,"strategy_runs",len(forecasts))
        self._increment_symbol_status(symbol,"signals_generated",len(forecasts))
        completed_ns=time.perf_counter_ns()
        self._record_timing_sample(symbol,{
            "message_parse_ms":parse_ms,
            "data_processing_ms":data_processing_ms,
            "strategy_evaluation_ms":strategy_evaluation_ms,
            "signal_persistence_ms":signal_persistence_ms,
            "collector_processing_ms":(completed_ns-processing_started_ns)/1_000_000,
            "tick_to_forecast_ms":(completed_ns-received_at_ns)/1_000_000,
        })
        if len(self.quote_tasks) >= self.settings.max_pending_quote_tasks:
            self._record_error("Proposal queue full; forecast retained without payout quote")
            log.warning("Proposal queue full; skipping quote requests for %s tick %s",symbol,tick_id)
        else:
            task=asyncio.create_task(self._capture_quotes(symbol,tick_id,int(tick["epoch"]),forecast_rows))
            self.quote_tasks.add(task)
            task.add_done_callback(self.quote_tasks.discard)
        return True

    def _persist_quote_results(self, symbol, tick_id, epoch, forecast_rows, quotes):
        for forecast,seq in forecast_rows:
            for horizon in (1,2,3,4,5):
                quote=quotes.get((symbol,int(forecast.prediction),int(horizon)))
                if not quote:
                    continue
                try:
                    # Preserve the existing one-tick paper path; longer horizons
                    # remain separate research-only records.
                    if horizon==1:
                        self.store.append_quote(
                            symbol,forecast,seq,quote["payout"],quote["stake"],
                            quote["proposal_id"],quote.get("spot_time")
                        )
                        if self.paperbot:
                            self.paperbot.record(
                                symbol,forecast,seq,tick_id,epoch,quote["stake"],
                                quote["payout"],quote["proposal_id"],quote["spot_time"]
                            )
                    self.store.append_duration_quote(
                        symbol,forecast,seq,tick_id,epoch,horizon,quote["stake"],
                        quote["payout"],quote["proposal_id"],quote.get("spot_time")
                    )
                except Exception as exc:
                    self._record_error(
                        f"Duration quote persistence failed for {symbol} forecast {seq} horizon {horizon}: {exc}"
                    )
                    log.exception("Could not persist duration quote")

    async def _capture_quotes(self, symbol, tick_id, epoch, forecast_rows):
        try:
            # Request actual Deriv proposal quotes for every candidate digit and
            # horizon over one WebSocket session to minimize quote latency.
            unique=sorted({forecast.prediction for forecast,_ in forecast_rows})
            requests=[(symbol,d,self.settings.paper_stake,horizon)
                      for d in unique for horizon in (1,2,3,4,5)]
            try:
                async with self.proposal_semaphore:
                    quotes=await self.proposals.quote_many(requests)
            except ProposalUnavailable as exc:
                self._record_error(f"Duration proposal batch unavailable for {symbol}: {exc}")
                quotes={}
            async with self.quote_persistence_lock:
                await asyncio.to_thread(
                    self._persist_quote_results,symbol,tick_id,epoch,forecast_rows,quotes
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(f"Quote capture task failed for {symbol} tick {tick_id}: {exc}")
            log.exception("Quote capture task failed")
    async def run_forever(self):
        delay=1
        try:
            while True:
                connected_at=None
                try:
                    self._set_connection_status("CONNECTING")
                    async with websockets.connect(self.settings.ws_url,ping_interval=20,ping_timeout=20,close_timeout=10) as ws:
                        connected_at=time.monotonic()
                        self.connection_started_monotonic=connected_at
                        self._set_connection_status("CONNECTED")
                        req_symbols={}; subscription_symbols={}
                        # Seed from the latest historical ticks without generating retrospective forecasts.
                        # This avoids waiting 1,000 live seconds before the research engine can start.
                        history_requests={200+index:symbol for index,symbol in enumerate(self.settings.symbols)}
                        history_status={symbol:"PENDING" for symbol in self.settings.symbols}
                        pending=set(history_requests)
                        for req_id,symbol in history_requests.items():
                            await ws.send(json.dumps({"ticks_history":symbol,"count":1000,"end":"latest","style":"ticks","req_id":req_id}))
                        while pending:
                            try:
                                raw_history=await asyncio.wait_for(ws.recv(),timeout=8)
                            except asyncio.TimeoutError:
                                self._record_error("Historical seed timed out; continuing with live ticks only")
                                break
                            symbol=None
                            try:
                                seed_msg=json.loads(raw_history,parse_float=Decimal)
                                seed_id=seed_msg.get("req_id")
                                if seed_id not in pending:
                                    continue
                                pending.remove(seed_id)
                                symbol=history_requests[seed_id]
                                if seed_msg.get("error"):
                                    history_status[symbol]="ERROR"
                                    self._update_symbol_status(symbol,history_seed_status="ERROR")
                                    error_message=seed_msg["error"].get("message","Deriv error")
                                    self._record_error(f"Historical seed failed for {symbol}: {error_message}",symbol)
                                    continue
                                hist=seed_msg.get("history") or {}
                                inserted=self._seed_history(symbol,hist,seed_msg.get("pip_size"))
                                history_status[symbol]="READY"
                                self._update_symbol_status(symbol,history_seed_status="READY")
                                log.info("Seeded %s historical ticks for %s",inserted,symbol)
                            except (json.JSONDecodeError,QuotePrecisionError,ValueError,KeyError,TypeError) as exc:
                                if symbol is not None:
                                    history_status[symbol]="ERROR"
                                    self._update_symbol_status(symbol,history_seed_status="ERROR")
                                self._record_error(f"Historical seed rejected: {exc}",symbol)
                                log.exception("Could not seed historical ticks")
                        for symbol in self.settings.symbols:
                            self.cumulative_counts[symbol]=list(self.store.digit_counts(symbol))
                            if symbol in pending:
                                history_status[symbol]="TIMEOUT"
                            if history_status[symbol]!="READY":
                                self._update_symbol_status(symbol,history_seed_status=history_status[symbol])
                        for index,symbol in enumerate(self.settings.symbols):
                            req_id=100+index; req_symbols[req_id]=symbol
                            self._update_symbol_status(
                                symbol,
                                subscription_status="SUBSCRIBING",
                            )
                            self._increment_symbol_status(symbol,"subscription_count")
                            await ws.send(json.dumps({"ticks":symbol,"subscribe":1,"req_id":req_id}))
                        # A normal WebSocket close is also a disconnect; route it through
                        # the same bounded backoff path as network errors.
                        async for raw in ws:
                            message_received_ns=time.perf_counter_ns()
                            try:
                                parse_started_ns=time.perf_counter_ns()
                                msg=json.loads(raw,parse_float=Decimal)
                                parse_ms=(time.perf_counter_ns()-parse_started_ns)/1_000_000
                                if msg.get("error"):
                                    self._record_error(msg["error"].get("message","Deriv error"))
                                    log.error("Deriv stream error: %s",msg["error"])
                                    continue
                                tick=msg.get("tick")
                                if msg.get("msg_type")=="tick" and tick:
                                    req_id=msg.get("req_id")
                                    subscription_id=(msg.get("subscription") or {}).get("id")
                                    hint=(tick.get("symbol") or req_symbols.get(req_id)
                                          or (msg.get("echo_req") or {}).get("ticks")
                                          or subscription_symbols.get(subscription_id)
                                          or subscription_symbols.get(tick.get("id")))
                                    if hint:
                                        if subscription_id: subscription_symbols[subscription_id]=hint
                                        if tick.get("id"): subscription_symbols[tick["id"]]=hint
                                    try:
                                        await self._handle_tick(tick,hint,message_received_ns,parse_ms)
                                    except (QuotePrecisionError,ValueError,KeyError,TypeError) as exc:
                                        self._record_error(f"Rejected malformed tick: {exc}",hint)
                                        log.exception("Rejected malformed tick: %s",exc)
                                    except Exception as exc:
                                        self._record_error(f"Tick processing failed: {exc}",hint)
                                        self._update_symbol_status(
                                            hint,
                                            analysis_status="ERROR",
                                            strategy_status="ERROR",
                                            signal_generation_status="ERROR",
                                        )
                                        log.exception("Tick processing failed")
                            except (json.JSONDecodeError,QuotePrecisionError,ValueError,KeyError,TypeError) as exc:
                                self._record_error(exc)
                                log.exception("Rejected malformed tick: %s",exc)
                    if connected_at is not None and time.monotonic()-connected_at>=60:
                        delay=1
                    self._set_connection_status("RECONNECTING","Deriv closed the tick stream",reconnect=True)
                    log.warning("Deriv closed the tick stream; reconnecting in %ss",delay)
                    await asyncio.sleep(delay)
                    delay=min(delay*2,30)
                except asyncio.CancelledError:
                    self._set_connection_status("DISCONNECTED")
                    raise
                except Exception as exc:
                    self._record_error(exc)
                    if connected_at is not None and time.monotonic()-connected_at>=60:
                        delay=1
                    self._set_connection_status("RECONNECTING",str(exc),reconnect=True)
                    log.exception("Market stream disconnected; reconnecting in %ss",delay)
                    await asyncio.sleep(delay)
                    delay=min(delay*2,30)
                finally:
                    self.connection_started_monotonic=None
        finally:
            if self.connection_status!="DISCONNECTED":
                self._set_connection_status("DISCONNECTED")
            for task in tuple(self.quote_tasks): task.cancel()
            if self.quote_tasks:
                await asyncio.gather(*tuple(self.quote_tasks),return_exceptions=True)
