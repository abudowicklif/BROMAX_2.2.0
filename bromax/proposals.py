import asyncio, json, itertools
from decimal import Decimal
import websockets

class ProposalUnavailable(RuntimeError): pass

def encode_proposal_request(symbol, digit, stake, currency, duration_ticks, req_id):
    if isinstance(stake,float): raise ValueError("Binary float stake is not accepted; pass Decimal")
    stake=Decimal(stake)
    if not stake.is_finite() or stake <= 0: raise ValueError("Proposal stake must be a positive finite Decimal")
    request={"proposal":1,"amount":"__BROMAX_DECIMAL_AMOUNT__","basis":"stake","contract_type":"DIGITMATCH",
             "currency":currency,"duration":duration_ticks,"duration_unit":"t",
             "barrier":str(digit),"underlying_symbol":symbol,"req_id":req_id}
    # Emit Decimal as an exact JSON number; do not round money through binary float.
    return json.dumps(request).replace('"__BROMAX_DECIMAL_AMOUNT__"',format(stake,"f"))

class ProposalClient:
    """Unauthenticated quote-only proposal client. It never calls buy/sell."""
    def __init__(self, ws_url, currency="USD", timeout=4.0):
        self.ws_url, self.currency, self.timeout = ws_url, currency, timeout
        self._ids = itertools.count(1000)
    async def quote(self, symbol: str, digit: int, stake: Decimal, duration_ticks: int = 1):
        req_id=next(self._ids)
        request=encode_proposal_request(symbol,digit,stake,self.currency,duration_ticks,req_id)
        try:
            async with websockets.connect(self.ws_url,ping_interval=20,ping_timeout=20,close_timeout=3) as ws:
                await ws.send(request)
                async with asyncio.timeout(self.timeout):
                    while True:
                        msg=json.loads(await ws.recv(),parse_float=Decimal)
                        if msg.get("req_id") != req_id: continue
                        if msg.get("error"): raise ProposalUnavailable(msg["error"].get("message","proposal error"))
                        p=msg.get("proposal")
                        if p:
                            payout=Decimal(str(p["payout"])); ask=Decimal(str(p["ask_price"])); spot_time=int(p["spot_time"])
                            if payout <= 0 or ask <= 0 or spot_time <= 0: raise ProposalUnavailable("Invalid proposal payout, ask price, or spot_time")
                            return {"payout":payout,"stake":ask,"proposal_id":str(p.get("id","")),"spot":str(p.get("spot","")),"spot_time":spot_time}
        except Exception as exc:
            if isinstance(exc, ProposalUnavailable): raise
            raise ProposalUnavailable(str(exc)) from exc

    async def quote_many(self, requests):
        """Fetch many read-only proposals over one WebSocket connection.

        requests: iterable of (symbol, digit, Decimal stake, duration_ticks).
        A single connection reduces latency for 1-second tick quote capture.
        """
        requests=list(requests)
        if not requests: return {}
        pending={}; output={}
        try:
            async with websockets.connect(self.ws_url,ping_interval=20,ping_timeout=20,close_timeout=3) as ws:
                for symbol,digit,stake,duration in requests:
                    req_id=next(self._ids)
                    payload=encode_proposal_request(symbol,digit,stake,self.currency,duration,req_id)
                    pending[req_id]=(symbol,int(digit),int(duration))
                    await ws.send(payload)
                async with asyncio.timeout(self.timeout):
                    while pending:
                        msg=json.loads(await ws.recv(),parse_float=Decimal)
                        req_id=msg.get('req_id')
                        if req_id not in pending: continue
                        key=pending.pop(req_id)
                        if msg.get('error'):
                            output[key]=None; continue
                        p=msg.get('proposal')
                        if not p:
                            output[key]=None; continue
                        payout=Decimal(str(p['payout'])); ask=Decimal(str(p['ask_price'])); spot_time=int(p['spot_time'])
                        if payout<=0 or ask<=0 or spot_time<=0:
                            output[key]=None; continue
                        output[key]={'payout':payout,'stake':ask,'proposal_id':str(p.get('id','')),'spot':str(p.get('spot','')),'spot_time':spot_time}
            return output
        except Exception as exc:
            raise ProposalUnavailable(str(exc)) from exc
