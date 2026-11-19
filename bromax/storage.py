import json, sqlite3
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS ticks(
 id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, epoch INTEGER NOT NULL,
 quote_text TEXT NOT NULL, pip_size INTEGER NOT NULL, digit INTEGER, received_utc TEXT NOT NULL,
 source_tick_id TEXT, UNIQUE(symbol, epoch)
);
CREATE INDEX IF NOT EXISTS ix_ticks_symbol_id ON ticks(symbol,id);
CREATE TABLE IF NOT EXISTS research_snapshots(
 symbol TEXT PRIMARY KEY, created_utc TEXT NOT NULL, n_ticks INTEGER NOT NULL,
 max_tick_id INTEGER, report_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_snapshots_no_update BEFORE UPDATE ON research_snapshots BEGIN SELECT RAISE(ABORT,'research snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_snapshots_no_delete BEFORE DELETE ON research_snapshots BEGIN SELECT RAISE(ABORT,'research snapshots are immutable'); END;
CREATE TABLE IF NOT EXISTS research_snapshots_v2(
 symbol TEXT NOT NULL, protocol_version TEXT NOT NULL, created_utc TEXT NOT NULL,
 n_ticks INTEGER NOT NULL, max_tick_id INTEGER, report_json TEXT NOT NULL,
 PRIMARY KEY(symbol,protocol_version)
);
CREATE TRIGGER IF NOT EXISTS research_snapshots_v2_no_update BEFORE UPDATE ON research_snapshots_v2 BEGIN SELECT RAISE(ABORT,'versioned research snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_snapshots_v2_no_delete BEFORE DELETE ON research_snapshots_v2 BEGIN SELECT RAISE(ABORT,'versioned research snapshots are immutable'); END;
CREATE TABLE IF NOT EXISTS agent_runs(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL UNIQUE,
 created_utc TEXT NOT NULL, symbol TEXT NOT NULL, snapshot_tick_id INTEGER,
 final_status TEXT NOT NULL, decision_key TEXT NOT NULL, agent_results_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_symbol_seq ON agent_runs(symbol,seq);
CREATE TRIGGER IF NOT EXISTS agent_runs_no_update BEFORE UPDATE ON agent_runs BEGIN SELECT RAISE(ABORT,'agent runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agent_runs_no_delete BEFORE DELETE ON agent_runs BEGIN SELECT RAISE(ABORT,'agent runs are immutable'); END;
CREATE TABLE IF NOT EXISTS audit_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 event_type TEXT NOT NULL CHECK(event_type IN ('FORECAST','SCORE','QUOTE','PAPER_OPEN','PAPER_SETTLE')),
 created_utc TEXT NOT NULL, symbol TEXT NOT NULL, strategy_id TEXT NOT NULL,
 strategy_version TEXT NOT NULL, parameters_json TEXT NOT NULL,
 forecast_tick_id INTEGER, forecast_epoch INTEGER, prediction INTEGER,
 probabilities_json TEXT, payout TEXT, stake TEXT, proposal_id TEXT, quote_spot_time TEXT,
 actual_tick_id INTEGER, actual_digit INTEGER, hit INTEGER,
 paper_pnl TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_symbol_strategy ON audit_events(symbol,strategy_id,seq);
CREATE INDEX IF NOT EXISTS ix_events_type_note ON audit_events(event_type,note);
CREATE INDEX IF NOT EXISTS ix_events_symbol_type_seq ON audit_events(symbol,event_type,seq);
CREATE INDEX IF NOT EXISTS ix_events_symbol_type_epoch_seq ON audit_events(symbol,event_type,forecast_epoch,seq);
CREATE TABLE IF NOT EXISTS duration_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 event_type TEXT NOT NULL CHECK(event_type IN ('QUOTE','SETTLE')),
 created_utc TEXT NOT NULL, symbol TEXT NOT NULL, strategy_id TEXT NOT NULL,
 strategy_version TEXT NOT NULL, forecast_seq INTEGER NOT NULL, forecast_tick_id INTEGER NOT NULL,
 forecast_epoch INTEGER NOT NULL, prediction INTEGER NOT NULL, duration_ticks INTEGER NOT NULL,
 stake TEXT, payout TEXT, proposal_id TEXT, quote_spot_time TEXT,
 actual_tick_id INTEGER, actual_epoch INTEGER, actual_digit INTEGER, hit INTEGER, paper_pnl TEXT,
 UNIQUE(event_type,forecast_seq,duration_ticks)
);
CREATE INDEX IF NOT EXISTS ix_duration_symbol ON duration_events(symbol,duration_ticks,forecast_epoch);
CREATE TRIGGER IF NOT EXISTS duration_events_no_update BEFORE UPDATE ON duration_events BEGIN SELECT RAISE(ABORT,'duration_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS duration_events_no_delete BEFORE DELETE ON duration_events BEGIN SELECT RAISE(ABORT,'duration_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ticks_no_update BEFORE UPDATE ON ticks BEGIN SELECT RAISE(ABORT,'ticks is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ticks_no_delete BEFORE DELETE ON ticks BEGIN SELECT RAISE(ABORT,'ticks is append-only'); END;
"""

def utc_now(): return datetime.now(timezone.utc).isoformat()

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()

class Store:
    def __init__(self, path: str):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c: c.executescript(SCHEMA)
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection); c.row_factory = sqlite3.Row; return c
    def add_tick(self, symbol, epoch, quote_text, pip_size, digit, source_tick_id=None):
        with self.connect() as c:
            cur = c.execute("INSERT OR IGNORE INTO ticks(symbol,epoch,quote_text,pip_size,digit,received_utc,source_tick_id) VALUES(?,?,?,?,?,?,?)",
                (symbol,int(epoch),quote_text,int(pip_size),digit,utc_now(),source_tick_id))
            if cur.rowcount == 0: return None
            return cur.lastrowid
    def history(self, symbol, limit=1000):
        with self.connect() as c:
            rows = c.execute("SELECT id,epoch,digit,quote_text FROM ticks WHERE symbol=? AND digit IS NOT NULL ORDER BY epoch DESC,id DESC LIMIT ?",(symbol,limit)).fetchall()
        return list(reversed(rows))
    def digit_counts(self, symbol):
        with self.connect() as c:
            rows=c.execute("SELECT digit,COUNT(*) AS n FROM ticks WHERE symbol=? AND digit IS NOT NULL GROUP BY digit",(symbol,)).fetchall()
        counts=[0]*10
        for r in rows: counts[int(r["digit"])]=int(r["n"])
        return tuple(counts)
    def append_forecast(self, symbol, forecast, tick_id, epoch, payout=None, stake=None, proposal_id=None, note=None):
        import uuid
        with self.connect() as c:
            cur = c.execute("""INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,probabilities_json,payout,stake,proposal_id,note)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(str(uuid.uuid4()),"FORECAST",utc_now(),symbol,forecast.strategy_id,forecast.version,json.dumps(forecast.parameters,sort_keys=True),tick_id,epoch,forecast.prediction,json.dumps(forecast.probabilities),str(payout) if payout is not None else None,str(stake) if stake is not None else None,proposal_id,note))
            return cur.lastrowid
    def append_forecasts(self, symbol, forecasts, tick_id, epoch, payout=None, stake=None, proposal_id=None, note=None):
        import uuid
        seqs=[]
        with self.connect() as c:
            for forecast in forecasts:
                cur=c.execute("""INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,probabilities_json,payout,stake,proposal_id,note)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()),"FORECAST",utc_now(),symbol,forecast.strategy_id,forecast.version,
                     json.dumps(forecast.parameters,sort_keys=True),tick_id,epoch,forecast.prediction,
                     json.dumps(forecast.probabilities),str(payout) if payout is not None else None,
                     str(stake) if stake is not None else None,proposal_id,note))
                seqs.append(cur.lastrowid)
        return seqs
    def score_open_forecasts(self, symbol, actual_tick_id, actual_epoch, actual_digit):
        import uuid
        with self.connect() as c:
            rows = c.execute("SELECT * FROM audit_events f WHERE f.event_type='FORECAST' AND f.symbol=? AND f.forecast_epoch=? AND NOT EXISTS (SELECT 1 FROM audit_events s WHERE s.event_type='SCORE' AND s.note=CAST(f.seq AS TEXT)) ORDER BY f.seq",(symbol,int(actual_epoch)-1)).fetchall()
            scored=[]
            for f in rows:
                # 1HZ... 1-second symbols: score only the immediately following epoch.
                # If a tick was missed, leave the forecast unscored; never score it against a later tick.
                if f["forecast_tick_id"] is None or actual_tick_id <= f["forecast_tick_id"]: continue
                hit = int(f["prediction"] == actual_digit)
                c.execute("INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,actual_tick_id,actual_digit,hit,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()),"SCORE",utc_now(),symbol,f["strategy_id"],f["strategy_version"],f["parameters_json"],f["forecast_tick_id"],f["forecast_epoch"],f["prediction"],actual_tick_id,actual_digit,hit,str(f["seq"])))
                
                opens=c.execute("SELECT * FROM audit_events WHERE event_type='PAPER_OPEN' AND note=?",(str(f["seq"]),)).fetchall()
                for op in opens:
                    stake=Decimal(op["stake"]); payout=Decimal(op["payout"]); pnl=(payout-stake) if hit else -stake
                    c.execute("INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,actual_tick_id,actual_digit,hit,paper_pnl,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()),"PAPER_SETTLE",utc_now(),symbol,op["strategy_id"],op["strategy_version"],op["parameters_json"],op["forecast_tick_id"],op["forecast_epoch"],op["prediction"],actual_tick_id,actual_digit,hit,str(pnl),str(op["seq"])))
                scored.append((f["seq"],hit))
            return scored
    def append_quote(self, symbol, forecast, forecast_seq, payout, stake, proposal_id, spot_time=None):
        import uuid
        with self.connect() as c:
            c.execute("INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,payout,stake,proposal_id,quote_spot_time,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),"QUOTE",utc_now(),symbol,forecast.strategy_id,forecast.version,json.dumps(forecast.parameters,sort_keys=True),None,None,forecast.prediction,str(payout),str(stake),proposal_id,str(spot_time) if spot_time is not None else None,str(forecast_seq)))

    def append_paper_open(self, symbol, forecast, forecast_seq, tick_id, epoch, stake, payout, proposal_id, spot_time=None):
        import uuid
        with self.connect() as c:
            c.execute("INSERT INTO audit_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,parameters_json,forecast_tick_id,forecast_epoch,prediction,payout,stake,proposal_id,quote_spot_time,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),"PAPER_OPEN",utc_now(),symbol,forecast.strategy_id,forecast.version,json.dumps(forecast.parameters,sort_keys=True),tick_id,epoch,forecast.prediction,str(payout),str(stake),proposal_id,str(spot_time) if spot_time is not None else None,str(forecast_seq)))

    def is_latest_tick(self, symbol, tick_id):
        with self.connect() as c:
            row=c.execute("SELECT id FROM ticks WHERE symbol=? ORDER BY epoch DESC,id DESC LIMIT 1",(symbol,)).fetchone()
        return bool(row and row[0]==tick_id)

    def forecasts(self, symbol=None, limit=None):
        with self.connect() as c:
            q="""SELECT f.*,s.actual_digit AS scored_actual_digit,s.hit AS scored_hit,s.actual_tick_id AS scored_actual_tick_id,
            (SELECT q.payout FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS quoted_payout,
            (SELECT q.stake FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS quoted_stake,
            (SELECT q.proposal_id FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS proposal_id,
            (SELECT q.quote_spot_time FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS quoted_spot_time,
            (SELECT q.created_utc FROM audit_events q WHERE q.event_type='QUOTE' AND q.note=CAST(f.seq AS TEXT) AND (s.seq IS NULL OR q.seq<s.seq) ORDER BY q.seq DESC LIMIT 1) AS quote_created_utc
            FROM audit_events f LEFT JOIN audit_events s ON s.event_type='SCORE' AND s.note=CAST(f.seq AS TEXT) WHERE f.event_type='FORECAST'"""
            args=()
            if symbol: q += " AND f.symbol=?"; args=(symbol,)
            q += " ORDER BY f.seq DESC" if limit is not None else " ORDER BY f.seq"
            if limit is not None:
                q += " LIMIT ?"
                args = args + (max(1, int(limit)),)
            result=[]
            for r in c.execute(q,args).fetchall():
                d=dict(r); d['actual_digit']=d.pop('scored_actual_digit'); d['hit']=d.pop('scored_hit'); d['actual_tick_id']=d.pop('scored_actual_tick_id'); d['quote_spot_time']=d.pop('quoted_spot_time'); d['payout']=d.pop('quoted_payout'); d['stake']=d.pop('quoted_stake'); d['probabilities']=json.loads(d['probabilities_json']) if d.get('probabilities_json') else None
                # A quote is valid for this contract only if recorded before the forecast's next 1-second epoch.
                qtime=d.get('quote_created_utc')
                if qtime:
                    from datetime import datetime
                    try: quote_epoch=datetime.fromisoformat(qtime).timestamp()
                    except ValueError: quote_epoch=float('inf')
                    spot_time=d.get('quote_spot_time')
                    try: spot_epoch=int(spot_time) if spot_time is not None else None
                    except (TypeError,ValueError): spot_epoch=None
                    if quote_epoch >= int(d['forecast_epoch'])+1 or spot_epoch != int(d['forecast_epoch']):
                        d['payout']=None; d['stake']=None; d['proposal_id']=None
                result.append(d)
            if limit is not None:
                result.reverse()
            return result

    def recent_forecasts(self, symbol, limit=50):
        with self.connect() as c:
            rows=c.execute("""SELECT f.seq,f.strategy_id,f.strategy_version,f.parameters_json,
                f.forecast_tick_id,f.forecast_epoch,f.prediction,f.probabilities_json,
                f.created_utc,s.actual_digit,s.hit,s.actual_tick_id
                FROM audit_events f
                LEFT JOIN audit_events s ON s.event_type='SCORE' AND s.note=CAST(f.seq AS TEXT)
                WHERE f.event_type='FORECAST' AND f.symbol=?
                ORDER BY f.forecast_epoch DESC,f.seq DESC LIMIT ?""",
                (symbol,max(1,int(limit)))).fetchall()
        result=[]
        for row in reversed(rows):
            item=dict(row)
            item["probabilities"]=json.loads(item.pop("probabilities_json")) if item.get("probabilities_json") else None
            item["version"]=item.pop("strategy_version")
            item["parameters"]=json.loads(item.pop("parameters_json")) if item.get("parameters_json") else {}
            result.append(item)
        return result

    def append_duration_quote(self, symbol, forecast, forecast_seq, tick_id, epoch, duration, stake, payout, proposal_id, spot_time):
        import uuid
        with self.connect() as c:
            # Quotes are retained only when Deriv priced the same forecast tick and
            # the quote arrived within that tick's entry window.
            import time
            if spot_time is None or int(spot_time) != int(epoch) or time.time() >= int(epoch)+1: return False
            c.execute("INSERT OR IGNORE INTO duration_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,forecast_seq,forecast_tick_id,forecast_epoch,prediction,duration_ticks,stake,payout,proposal_id,quote_spot_time) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),'QUOTE',utc_now(),symbol,forecast.strategy_id,forecast.version,int(forecast_seq),int(tick_id),int(epoch),int(forecast.prediction),int(duration),str(stake),str(payout),str(proposal_id),str(spot_time)))
            return True

    def settle_duration_contracts(self, symbol, tick_id, epoch, digit):
        import uuid
        with self.connect() as c:
            rows=c.execute("""SELECT q.* FROM duration_events q WHERE q.event_type='QUOTE' AND q.symbol=?
                AND q.forecast_epoch+q.duration_ticks=? AND NOT EXISTS
                (SELECT 1 FROM duration_events s WHERE s.event_type='SETTLE' AND s.forecast_seq=q.forecast_seq AND s.duration_ticks=q.duration_ticks)""",(symbol,int(epoch))).fetchall()
            settled=[]
            for q in rows:
                # Exact epoch equality ensures no missing-tick/look-ahead substitution.
                hit=int(int(q['prediction'])==int(digit)); stake=Decimal(q['stake']); payout=Decimal(q['payout'])
                pnl=(payout-stake) if hit else -stake
                c.execute("INSERT INTO duration_events(event_id,event_type,created_utc,symbol,strategy_id,strategy_version,forecast_seq,forecast_tick_id,forecast_epoch,prediction,duration_ticks,stake,payout,proposal_id,quote_spot_time,actual_tick_id,actual_epoch,actual_digit,hit,paper_pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()),'SETTLE',utc_now(),symbol,q['strategy_id'],q['strategy_version'],q['forecast_seq'],q['forecast_tick_id'],q['forecast_epoch'],q['prediction'],q['duration_ticks'],q['stake'],q['payout'],q['proposal_id'],q['quote_spot_time'],int(tick_id),int(epoch),int(digit),hit,str(pnl)))
                settled.append({'forecast_seq':q['forecast_seq'],'duration_ticks':q['duration_ticks'],'hit':hit,'pnl':str(pnl)})
            return settled

    def duration_events(self, symbol=None):
        with self.connect() as c:
            if symbol:
                rows=c.execute("SELECT * FROM duration_events WHERE symbol=? ORDER BY seq",(symbol,)).fetchall()
            else: rows=c.execute("SELECT * FROM duration_events ORDER BY seq").fetchall()
            return [dict(r) for r in rows]

    def duration_quote(self, forecast_seq, duration_ticks):
        with self.connect() as c:
            row=c.execute("SELECT * FROM duration_events WHERE event_type='QUOTE' AND forecast_seq=? AND duration_ticks=? ORDER BY seq DESC LIMIT 1",(int(forecast_seq),int(duration_ticks))).fetchone()
        return dict(row) if row else None

    def research_snapshot(self, symbol):
        with self.connect() as c:
            row=c.execute("SELECT * FROM research_snapshots WHERE symbol=?",(symbol,)).fetchone()
        return dict(row) if row else None

    def freeze_research_snapshot(self, symbol, n_ticks, max_tick_id, report_json):
        with self.connect() as c:
            cur=c.execute("INSERT OR IGNORE INTO research_snapshots(symbol,created_utc,n_ticks,max_tick_id,report_json) VALUES(?,?,?,?,?)",
                (symbol,utc_now(),int(n_ticks),max_tick_id,report_json))
            return cur.rowcount == 1

    def research_snapshot_versioned(self, symbol, protocol_version):
        with self.connect() as c:
            row=c.execute("SELECT * FROM research_snapshots_v2 WHERE symbol=? AND protocol_version=?",(symbol,protocol_version)).fetchone()
        return dict(row) if row else None

    def freeze_research_snapshot_versioned(self, symbol, protocol_version, n_ticks, max_tick_id, report_json):
        with self.connect() as c:
            cur=c.execute("INSERT OR IGNORE INTO research_snapshots_v2(symbol,protocol_version,created_utc,n_ticks,max_tick_id,report_json) VALUES(?,?,?,?,?,?)",
                (symbol,protocol_version,utc_now(),int(n_ticks),max_tick_id,report_json))
            return cur.rowcount == 1

    def paper_events(self, symbol=None):
        with self.connect() as c:
            q="SELECT * FROM audit_events WHERE event_type IN ('PAPER_OPEN','PAPER_SETTLE')"
            args=()
            if symbol: q += " AND symbol=?"; args=(symbol,)
            q += " ORDER BY seq"
            return [dict(r) for r in c.execute(q,args).fetchall()]

    def quality_history(self, symbol, limit=20):
        with self.connect() as c:
            rows=c.execute(
                "SELECT id,epoch,quote_text,pip_size,digit,received_utc FROM ticks WHERE symbol=? ORDER BY epoch DESC,id DESC LIMIT ?",
                (symbol,max(1,int(limit))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_agent_run(self, snapshot_id, symbol, snapshot_tick_id, final_status, decision_key, agent_results, minimum_interval_seconds=60):
        now=datetime.now(timezone.utc)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            latest=c.execute(
                "SELECT created_utc,final_status,decision_key FROM agent_runs WHERE symbol=? ORDER BY seq DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if latest and latest["final_status"]==final_status and latest["decision_key"]==decision_key:
                previous=datetime.fromisoformat(latest["created_utc"])
                if (now-previous).total_seconds()<minimum_interval_seconds:
                    return False
            c.execute(
                "INSERT INTO agent_runs(snapshot_id,created_utc,symbol,snapshot_tick_id,final_status,decision_key,agent_results_json) VALUES(?,?,?,?,?,?,?)",
                (snapshot_id,now.isoformat(),symbol,snapshot_tick_id,final_status,decision_key,json.dumps(agent_results,separators=(",",":"),default=str)),
            )
            return True

    def agent_runs(self, symbol, limit=20):
        with self.connect() as c:
            rows=c.execute(
                "SELECT seq,snapshot_id,created_utc,symbol,snapshot_tick_id,final_status,decision_key,agent_results_json "
                "FROM agent_runs WHERE symbol=? ORDER BY seq DESC LIMIT ?",
                (symbol,max(1,int(limit))),
            ).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            item["agent_results"]=json.loads(item.pop("agent_results_json"))
            result.append(item)
        return result

    def ticks_frame(self, symbol):
        import pandas as pd
        with self.connect() as c:
            rows=c.execute("SELECT id,epoch,digit,quote_text FROM ticks WHERE symbol=? AND digit IS NOT NULL ORDER BY epoch,id",(symbol,)).fetchall()
        return pd.DataFrame([dict(r) for r in rows])
