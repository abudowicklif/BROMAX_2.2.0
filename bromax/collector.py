import asyncio, json, logging, time
from decimal import Decimal
import websockets
from .digits import extract_last_digit, quote_precision, QuotePrecisionError
from .strategies import candidate_strategies, ForecastContext
from .proposals import ProposalClient, ProposalUnavailable
from .paperbot import PaperBot

log=logging.getLogger("bromax.collector")

class MarketCollector:
    def __init__(self, settings, store, paperbot=None):
        self.settings=settings; self.store=store; self.paperbot=paperbot; self.last_received={}; self.errors=[]
        self.proposals=ProposalClient(settings.ws_url,settings.currency,settings.proposal_timeout_seconds)
        self.proposal_semaphore=asyncio.Semaphore(3)
        self.quote_tasks=set()
        self.cumulative_counts={symbol:list(store.digit_counts(symbol)) for symbol in settings.symbols}
        self.strategies=candidate_strategies()

    def _record_error(self, message):
        self.errors.append(str(message))
        if len(self.errors)>100: del self.errors[:-100]

    def _seed_history(self, symbol, history, pip_size):
        """Persist a Deriv ticks_history response without generating retrospective forecasts."""
        prices=history.get("prices") or []
        times=history.get("times") or []
        precision=quote_precision(pip_size)
        if len(prices)!=len(times):
            raise ValueError(f"Historical seed arrays have mismatched lengths for {symbol}")
        inserted=0
        for price,epoch in zip(prices,times):
            quote_text,digit=extract_last_digit(price,precision)
            if digit is not None and self.store.add_tick(symbol,int(epoch),quote_text,precision,digit) is not None:
                inserted += 1
        return inserted
    async def _handle_tick(self, tick, symbol_hint=None):
        symbol=tick.get("symbol") or symbol_hint
        if symbol not in self.settings.symbols: raise ValueError(f"Unexpected symbol {symbol!r}")
        if "quote" not in tick or "epoch" not in tick or "pip_size" not in tick: raise ValueError("Tick missing quote/epoch/pip_size; refusing to infer")
        precision=quote_precision(tick["pip_size"])
        quote_text,digit=extract_last_digit(tick["quote"],precision)
        if digit is None: raise ValueError(f"Symbol {symbol} has no displayed decimal digit for MATCH contracts")
        tick_id=self.store.add_tick(symbol,tick["epoch"],quote_text,precision,digit,tick.get("id"))
        if tick_id is None: return
        self.last_received[symbol]=time.time()
        self.cumulative_counts[symbol][digit] += 1
        # First score prior forecasts with this newly arrived tick; then forecast from history ending here.
        self.store.score_open_forecasts(symbol,tick_id,int(tick["epoch"]),digit)
        self.store.settle_duration_contracts(symbol,tick_id,int(tick["epoch"]),digit)
        history=self.store.history(symbol,limit=1000)
        digits=tuple(int(r["digit"]) for r in history)
        if len(digits)<self.settings.min_history_ticks: return
        latest=history[-1]
        ctx=ForecastContext(symbol,int(latest["id"]),int(latest["epoch"]),digits,tuple(self.cumulative_counts[symbol]))
        forecasts=[s.forecast(ctx) for s in self.strategies]
        # Freeze all forecasts synchronously before returning to the stream reader.
        # Quote requests run in background and are separately timestamped append-only events.
        forecast_rows=[]
        for f in forecasts:
            seq=self.store.append_forecast(symbol,f,tick_id,int(tick["epoch"]),None,self.settings.paper_stake,None,"PAYOUT_PENDING")
            forecast_rows.append((f,seq))
        if len(self.quote_tasks) >= self.settings.max_pending_quote_tasks:
            self._record_error("Proposal queue full; forecast retained without payout quote")
            log.warning("Proposal queue full; skipping quote requests for %s tick %s",symbol,tick_id)
        else:
            task=asyncio.create_task(self._capture_quotes(symbol,tick_id,int(tick["epoch"]),forecast_rows))
            self.quote_tasks.add(task)
            task.add_done_callback(self.quote_tasks.discard)

    async def _capture_quotes(self, symbol, tick_id, epoch, forecast_rows):
        unique=sorted({f.prediction for f,_ in forecast_rows})
        async def getq(d):
            try:
                async with self.proposal_semaphore:
                    return d, await self.proposals.quote(symbol,d,self.settings.paper_stake,1)
            except ProposalUnavailable as e:
                self._record_error(f"Proposal unavailable for {symbol} digit {d}: {e}")
                log.warning("No proposal for %s digit %s: %s",symbol,d,e); return d,None
        try:
            # Request actual Deriv proposal quotes for every candidate digit and
            # horizon over one WebSocket session to minimize quote latency.
            requests=[(symbol,d,self.settings.paper_stake,h) for d in unique for h in (1,2,3,4,5)]
            try:
                quotes=await self.proposals.quote_many(requests)
            except ProposalUnavailable as exc:
                self._record_error(f"Duration proposal batch unavailable for {symbol}: {exc}")
                quotes={}
            for f,seq in forecast_rows:
                for horizon in (1,2,3,4,5):
                    q=quotes.get((symbol,int(f.prediction),int(horizon)))
                    if not q: continue
                    try:
                        # Preserve legacy one-tick quote/paper path. Multi-tick
                        # quote/outcome pairs are tracked in their own immutable ledger.
                        if horizon == 1:
                            self.store.append_quote(symbol,f,seq,q["payout"],q["stake"],q["proposal_id"],q.get("spot_time"))
                            if self.paperbot:
                                self.paperbot.record(symbol,f,seq,tick_id,epoch,q["stake"],q["payout"],q["proposal_id"],q["spot_time"])
                        self.store.append_duration_quote(symbol,f,seq,tick_id,epoch,horizon,q["stake"],q["payout"],q["proposal_id"],q.get("spot_time"))
                    except Exception as exc:
                        self._record_error(f"Duration quote persistence failed for {symbol} forecast {seq} horizon {horizon}: {exc}")
                        log.exception("Could not persist duration quote")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(f"Quote capture task failed for {symbol} tick {tick_id}: {exc}")
            log.exception("Quote capture task failed")
    async def run_forever(self):
        delay=1
        try:
            while True:
                try:
                    async with websockets.connect(self.settings.ws_url,ping_interval=20,ping_timeout=20,close_timeout=10) as ws:
                        req_symbols={}; subscription_symbols={}
                        # Seed from the latest historical ticks without generating retrospective forecasts.
                        # This avoids waiting 1,000 live seconds before the research engine can start.
                        history_requests={200+index:symbol for index,symbol in enumerate(self.settings.symbols)}
                        pending=set(history_requests)
                        for req_id,symbol in history_requests.items():
                            await ws.send(json.dumps({"ticks_history":symbol,"count":1000,"end":"latest","style":"ticks","req_id":req_id}))
                        while pending:
                            try:
                                raw_history=await asyncio.wait_for(ws.recv(),timeout=8)
                            except asyncio.TimeoutError:
                                self._record_error("Historical seed timed out; continuing with live ticks only")
                                break
                            try:
                                seed_msg=json.loads(raw_history,parse_float=Decimal)
                                seed_id=seed_msg.get("req_id")
                                if seed_id not in pending:
                                    continue
                                pending.remove(seed_id)
                                symbol=history_requests[seed_id]
                                if seed_msg.get("error"):
                                    self._record_error(f"Historical seed failed for {symbol}: {seed_msg['error'].get('message','Deriv error')}")
                                    continue
                                hist=seed_msg.get("history") or {}
                                inserted=self._seed_history(symbol,hist,seed_msg.get("pip_size"))
                                log.info("Seeded %s historical ticks for %s",inserted,symbol)
                            except (json.JSONDecodeError,QuotePrecisionError,ValueError,KeyError,TypeError) as exc:
                                self._record_error(f"Historical seed rejected: {exc}")
                                log.exception("Could not seed historical ticks")
                        for symbol in self.settings.symbols:
                            self.cumulative_counts[symbol]=list(self.store.digit_counts(symbol))
                        for index,symbol in enumerate(self.settings.symbols):
                            req_id=100+index; req_symbols[req_id]=symbol
                            await ws.send(json.dumps({"ticks":symbol,"subscribe":1,"req_id":req_id}))
                        delay=1
                        async for raw in ws:
                            try:
                                msg=json.loads(raw,parse_float=Decimal)
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
                                    await self._handle_tick(tick,hint)
                            except (json.JSONDecodeError,QuotePrecisionError,ValueError,KeyError,TypeError) as exc:
                                self._record_error(exc)
                                log.exception("Rejected malformed tick: %s",exc)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._record_error(exc)
                    log.exception("Market stream disconnected; reconnecting in %ss",delay)
                    await asyncio.sleep(delay)
                    delay=min(delay*2,30)
        finally:
            for task in tuple(self.quote_tasks): task.cancel()
            if self.quote_tasks:
                await asyncio.gather(*tuple(self.quote_tasks),return_exceptions=True)
