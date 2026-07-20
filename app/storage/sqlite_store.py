from __future__ import annotations

import asyncio
import csv
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from app.storage.json_store import JsonCallStore

TERMINAL = {"COMPLETED", "FAILED", "CANCELED", "BUSY", "NO_ANSWER", "HUNG_UP", "NEEDS_RECONCILIATION"}
ACTIVE_JOB_STATES = ("queued", "claimed", "active", "canceling", "needs_reconciliation")

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

class SuppressedError(RuntimeError): pass
class DuplicateCallError(RuntimeError):
    def __init__(self, call_id: str): self.call_id = call_id

class SQLiteCallStore(JsonCallStore):
    """Transactional repository. A fresh connection is owned by every operation."""
    def __init__(self, database_path: Path, legacy_data_dir: Path | None = None) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = legacy_data_dir or self.database_path.parent
        super().__init__(self.data_dir)
        self._init_schema()
        self.migrate_legacy()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    def _init_schema(self) -> None:
        with self.transaction() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS leads(lead_id TEXT PRIMARY KEY,business_name TEXT NOT NULL,phone_number TEXT NOT NULL UNIQUE,category TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'new',do_not_call INTEGER NOT NULL DEFAULT 0,review_required INTEGER NOT NULL DEFAULT 0,last_call_id TEXT,last_call_sid TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls(call_id TEXT PRIMARY KEY,lead_id TEXT REFERENCES leads(lead_id),phone_number TEXT NOT NULL,business_name TEXT NOT NULL DEFAULT '',category TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',call_sid TEXT UNIQUE,lifecycle_state TEXT NOT NULL,provider_status TEXT,outcome TEXT,media_connected INTEGER NOT NULL DEFAULT 0,media_owner TEXT,transcript TEXT NOT NULL DEFAULT '',structured_response TEXT NOT NULL DEFAULT '{}',interested INTEGER NOT NULL DEFAULT 0,callback_requested INTEGER NOT NULL DEFAULT 0,do_not_call_requested INTEGER NOT NULL DEFAULT 0,extraction_status TEXT NOT NULL DEFAULT 'not_required',extraction_error TEXT,extraction_attempts INTEGER NOT NULL DEFAULT 0,started_at TEXT,ended_at TEXT,duration INTEGER NOT NULL DEFAULT 0,finalized_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS call_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,call_id TEXT NOT NULL REFERENCES calls(call_id),event_name TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT '{}',timestamp TEXT NOT NULL,UNIQUE(call_id,event_name,timestamp));
            CREATE TABLE IF NOT EXISTS call_jobs(job_id TEXT PRIMARY KEY,call_id TEXT NOT NULL UNIQUE REFERENCES calls(call_id),queue_state TEXT NOT NULL,lease_owner TEXT,lease_expires_at TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,idempotency_key TEXT UNIQUE,created_at TEXT NOT NULL,claimed_at TEXT,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS suppression_list(phone_number TEXT PRIMARY KEY,source_call_id TEXT REFERENCES calls(call_id),reason TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_phone ON calls(phone_number) WHERE lifecycle_state NOT IN ('COMPLETED','FAILED','CANCELED','BUSY','NO_ANSWER','HUNG_UP','NEEDS_RECONCILIATION');
            CREATE INDEX IF NOT EXISTS jobs_queue ON call_jobs(queue_state,created_at);
            CREATE INDEX IF NOT EXISTS calls_live ON calls(lifecycle_state,updated_at);
            """)

    @staticmethod
    def normalize_phone(value: Any) -> str:
        if not isinstance(value, (str, int, float)): raise ValueError("Phone number must be a scalar string")
        raw=str(value).strip(); digits=re.sub(r"\D", "", raw)
        if not raw: raise ValueError("Phone number is required")
        if not raw.startswith("+") and len(digits)==10: digits="91"+digits
        if not 8 <= len(digits) <= 15: raise ValueError("Phone number must be E.164 compatible")
        return "+"+digits

    @staticmethod
    def clean_text(value: Any, maximum: int) -> str:
        if value is None: return ""
        if not isinstance(value, str): raise ValueError("Lead fields must be strings")
        return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value)).strip()[:maximum]

    def validate_lead(self,row:dict[str,Any]):
        errors=[]
        try:
            normalized={"business_name":self.clean_text(row.get("business_name"),200),"phone_number":self.normalize_phone(row.get("phone_number")),"category":self.clean_text(row.get("category"),100),"notes":self.clean_text(row.get("notes"),1000)}
        except ValueError as exc: errors.append(str(exc)); normalized={"business_name":"","phone_number":"","category":"","notes":""}
        if not normalized["business_name"]: errors.append("Missing business name")
        return normalized,errors

    def import_leads(self,rows:list[dict[str,Any]])->dict[str,Any]:
        imported=[]; rejected=[]; duplicates=0; now=utcnow()
        with self.transaction(True) as db:
            for index,row in enumerate(rows,1):
                clean,errors=self.validate_lead(row)
                if errors: rejected.append({"row":index,"errors":errors,"lead":row}); continue
                lead={**clean,"lead_id":str(uuid4()),"status":"new","created_at":now,"updated_at":now}
                try: db.execute("INSERT INTO leads(lead_id,business_name,phone_number,category,notes,status,created_at,updated_at) VALUES(:lead_id,:business_name,:phone_number,:category,:notes,:status,:created_at,:updated_at)",lead); imported.append(lead)
                except sqlite3.IntegrityError: duplicates+=1; rejected.append({"row":index,"errors":["Duplicate phone number"],"lead":clean})
        return {"imported":len(imported),"duplicates":duplicates,"rejected":rejected,"total_submitted":len(rows),"leads":imported}

    def list_leads(self)->list[dict[str,Any]]: return self._rows("SELECT * FROM leads ORDER BY created_at DESC")
    def get_lead(self,lead_id:str): return self._one("SELECT * FROM leads WHERE lead_id=?",(lead_id,))
    def update_lead(self,lead_id:str,**updates:Any):
        allowed={"business_name","phone_number","category","notes","status","do_not_call","review_required","last_call_id","last_call_sid","last_error"}; values={k:v for k,v in updates.items() if k in allowed and v is not None}
        if not values:return self.get_lead(lead_id)
        values["updated_at"]=utcnow(); values["lead_id"]=lead_id
        with self.transaction(True) as db: db.execute(f"UPDATE leads SET {','.join(f'{k}=:{k}' for k in values if k!='lead_id')} WHERE lead_id=:lead_id",values)
        return self.get_lead(lead_id)

    def enqueue_call(self, *, phone_number:str, lead_id:str|None=None, business_name:str="", category:str="", notes:str="", idempotency_key:str|None=None)->dict[str,Any]:
        phone=self.normalize_phone(phone_number); now=utcnow(); call_id=str(uuid4())
        with self.transaction(True) as db:
            if idempotency_key:
                old=db.execute("SELECT call_id FROM call_jobs WHERE idempotency_key=?",(idempotency_key,)).fetchone()
                if old:return dict(db.execute("SELECT * FROM calls WHERE call_id=?",(old[0],)).fetchone())
            if db.execute("SELECT 1 FROM suppression_list WHERE phone_number=?",(phone,)).fetchone(): raise SuppressedError("Phone is on the do-not-call list")
            if lead_id:
                lead=db.execute("SELECT * FROM leads WHERE lead_id=?",(lead_id,)).fetchone()
                if not lead: raise KeyError("Lead not found")
                if lead["do_not_call"] or lead["review_required"]: raise SuppressedError("Lead is not callable")
            old=db.execute("SELECT call_id FROM calls WHERE phone_number=? AND lifecycle_state NOT IN (%s)" % ','.join('?'*len(TERMINAL)),(phone,*TERMINAL)).fetchone()
            if old:return dict(db.execute("SELECT * FROM calls WHERE call_id=?",(old[0],)).fetchone())
            db.execute("INSERT INTO calls(call_id,lead_id,phone_number,business_name,category,notes,lifecycle_state,extraction_status,created_at,updated_at) VALUES(?,?,?,?,?,?,'QUEUED','not_required',?,?)",(call_id,lead_id,phone,self.clean_text(business_name or "",200),self.clean_text(category or "",100),self.clean_text(notes or "",1000),now,now))
            db.execute("INSERT INTO call_jobs(job_id,call_id,queue_state,idempotency_key,created_at,updated_at) VALUES(?,?,'queued',?,?,?)",(call_id,call_id,idempotency_key,now,now))
            db.execute("INSERT INTO call_events(call_id,event_name,timestamp) VALUES(?,'queued',?)",(call_id,now))
            if lead_id: db.execute("UPDATE leads SET status='queued',last_call_id=?,updated_at=? WHERE lead_id=?",(call_id,now,lead_id))
        return self.get_call(call_id)

    def claim_job(self, owner:str, limit:int, lease_seconds:int=30):
        now=utcnow(); expiry=(datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction(True) as db:
            active=db.execute("SELECT COUNT(*) FROM call_jobs WHERE queue_state IN ('claimed','active','canceling','needs_reconciliation')").fetchone()[0]
            if active>=limit:return None
            row=db.execute("SELECT j.*,c.call_sid FROM call_jobs j JOIN calls c ON c.call_id=j.call_id WHERE j.queue_state='queued' OR (j.queue_state='claimed' AND j.lease_expires_at<? AND c.call_sid IS NULL) ORDER BY j.created_at LIMIT 1",(now,)).fetchone()
            if not row:return None
            db.execute("UPDATE call_jobs SET queue_state='claimed',lease_owner=?,lease_expires_at=?,claimed_at=COALESCE(claimed_at,?),attempt_count=attempt_count+1,updated_at=? WHERE job_id=?",(owner,expiry,now,now,row["job_id"]))
            db.execute("UPDATE calls SET lifecycle_state='CONNECTING',started_at=COALESCE(started_at,?),updated_at=? WHERE call_id=?",(now,now,row["call_id"]))
            return self.get_call(row["call_id"])

    def bind_call_sid(self,call_id:str,sid:str)->bool:
        with self.transaction(True) as db:
            row=db.execute("SELECT call_sid FROM calls WHERE call_id=?",(call_id,)).fetchone()
            if not row:return False
            if row[0] not in (None,sid):return False
            db.execute("UPDATE calls SET call_sid=?,provider_status=COALESCE(provider_status,'initiated'),updated_at=? WHERE call_id=?",(sid,utcnow(),call_id)); db.execute("UPDATE call_jobs SET queue_state='active',updated_at=? WHERE call_id=?",(utcnow(),call_id))
        return True

    def claim_media(self,call_id:str,sid:str,owner:str)->dict[str,Any]|None:
        with self.transaction(True) as db:
            row=db.execute("SELECT * FROM calls WHERE call_id=? AND call_sid=?",(call_id,sid)).fetchone()
            if not row or row["lifecycle_state"] in TERMINAL:return None
            changed=db.execute("UPDATE calls SET media_owner=?,media_connected=1,lifecycle_state='CONNECTED',provider_status='in-progress',updated_at=? WHERE call_id=? AND media_owner IS NULL",(owner,utcnow(),call_id)).rowcount
            if not changed:return None
        return self.get_call(call_id)

    def provider_status(self,call_id:str,status:str,sid:str|None=None)->dict[str,Any]|None:
        terminal={"busy":"BUSY","no-answer":"NO_ANSWER","failed":"FAILED","canceled":"CANCELED","completed":"COMPLETED"}; now=utcnow()
        with self.transaction(True) as db:
            row=db.execute("SELECT * FROM calls WHERE call_id=?",(call_id,)).fetchone()
            if not row:return None
            if sid and row["call_sid"] not in (None,sid):return None
            if sid and row["call_sid"] is None: db.execute("UPDATE calls SET call_sid=? WHERE call_id=?",(sid,call_id))
            if status in terminal and row["finalized_at"] is None:
                lifecycle=terminal[status]; outcome=status; extraction="pending" if row["media_connected"] else "not_required"
                db.execute("UPDATE calls SET provider_status=?,outcome=?,lifecycle_state=?,ended_at=COALESCE(ended_at,?),finalized_at=?,extraction_status=?,updated_at=? WHERE call_id=?",(status,outcome,lifecycle,now,now,extraction,now,call_id))
                db.execute("UPDATE call_jobs SET queue_state='terminal',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?",(now,call_id))
                if row["lead_id"]: db.execute("UPDATE leads SET status=?,last_call_id=?,last_call_sid=COALESCE(?,last_call_sid),updated_at=? WHERE lead_id=?",(outcome,call_id,sid,now,row["lead_id"]))
            elif status not in terminal and row["finalized_at"] is None: db.execute("UPDATE calls SET provider_status=?,updated_at=? WHERE call_id=?",(status,now,call_id))
            db.execute("INSERT INTO call_events(call_id,event_name,metadata,timestamp) VALUES(?,?,?,?)",(call_id,"provider_"+status,json.dumps({"status":status}),now))
        return self.get_call(call_id)

    def persist_raw(self,session:Any,status:str="completed"):
        now=utcnow(); meta=session.metadata or {}; connected=bool(session.transcript_text) or meta.get("media_connected",False); extraction="pending" if connected else "not_required"
        with self.transaction(True) as db:
            db.execute("INSERT OR IGNORE INTO calls(call_id,lead_id,phone_number,business_name,category,notes,lifecycle_state,created_at,updated_at) VALUES(?,?,?,?,?,?,'CREATED',?,?)",(session.call_id,meta.get("lead_id"),session.phone_number or meta.get("phone_number","") or "browser",meta.get("business_name","") or "",meta.get("category","") or "",meta.get("notes","") or "",session.started_at.isoformat(),now))
            row=db.execute("SELECT finalized_at,outcome FROM calls WHERE call_id=?",(session.call_id,)).fetchone(); outcome=row["outcome"] or status
            db.execute("UPDATE calls SET transcript=?,ended_at=COALESCE(ended_at,?),duration=?,outcome=?,lifecycle_state=CASE WHEN lifecycle_state IN ('BUSY','NO_ANSWER','FAILED','CANCELED') THEN lifecycle_state ELSE 'COMPLETED' END,extraction_status=CASE WHEN extraction_status='not_required' THEN ? ELSE extraction_status END,finalized_at=COALESCE(finalized_at,?),updated_at=? WHERE call_id=?",(session.transcript_text,(session.ended_at or datetime.now(timezone.utc)).isoformat(),session.duration_seconds,outcome,extraction,now,now,session.call_id))
            db.execute("UPDATE call_jobs SET queue_state='terminal',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?",(now,session.call_id))
            if meta.get("lead_id"):db.execute("UPDATE leads SET status=?,last_call_id=?,last_call_sid=COALESCE(?,last_call_sid),updated_at=? WHERE lead_id=?",(outcome,session.call_id,meta.get("call_sid"),now,meta["lead_id"]))
        return self.get_call(session.call_id)

    def save_extraction(self,call_id:str,answers:dict[str,Any]):
        now=utcnow(); interested=answers.get("account_creation_interest")=="yes"; callback=answers.get("callback_approved")=="yes"; dnd=answers.get("do_not_call_requested")=="yes"
        with self.transaction(True) as db:
            row=db.execute("SELECT lead_id,phone_number FROM calls WHERE call_id=?",(call_id,)).fetchone()
            db.execute("UPDATE calls SET structured_response=?,interested=?,callback_requested=?,do_not_call_requested=?,extraction_status='succeeded',extraction_error=NULL,extraction_attempts=extraction_attempts+1,updated_at=? WHERE call_id=?",(json.dumps(answers,ensure_ascii=False),interested,callback,dnd,now,call_id))
            if dnd and row:
                db.execute("INSERT OR IGNORE INTO suppression_list(phone_number,source_call_id,reason,created_at) VALUES(?,?,?,?)",(row["phone_number"],call_id,"customer_request",now))
                if row["lead_id"]:db.execute("UPDATE leads SET do_not_call=1,status='do_not_call',updated_at=? WHERE lead_id=?",(now,row["lead_id"]))

    def extraction_failed(self,call_id:str,error:str,permanent:bool=False):
        now=utcnow(); status="failed" if permanent else "retry_pending"
        with self.transaction(True) as db:
            row=db.execute("SELECT lead_id,media_connected FROM calls WHERE call_id=?",(call_id,)).fetchone()
            db.execute("UPDATE calls SET extraction_status=?,extraction_error=?,extraction_attempts=extraction_attempts+1,updated_at=? WHERE call_id=?",(status,error[:500],now,call_id))
            if permanent and row and row["lead_id"] and row["media_connected"]:db.execute("UPDATE leads SET review_required=1,status='review_required',last_error=?,updated_at=? WHERE lead_id=?",("Extraction requires operator review",now,row["lead_id"]))

    def list_calls(self,limit:int=200,offset:int=0): return self._rows("SELECT *,phone_number AS phone,outcome AS call_status FROM calls ORDER BY created_at DESC LIMIT ? OFFSET ?",(min(max(limit,1),500),max(offset,0)))
    def list_live_calls(self,limit:int=100): return self._rows("SELECT *,lifecycle_state AS status FROM calls WHERE lifecycle_state NOT IN (%s) ORDER BY updated_at DESC LIMIT ?" % ','.join('?'*len(TERMINAL)),(*TERMINAL,min(limit,100)))
    def get_call(self,call_id:str):
        row=self._one("SELECT *,phone_number AS phone,outcome AS call_status FROM calls WHERE call_id=?",(call_id,));
        if row: row["structured_response"]=json.loads(row.get("structured_response") or "{}")
        return row
    def _rows(self,sql:str,args:tuple=()):
        with self._connect() as db:return [dict(x) for x in db.execute(sql,args).fetchall()]
    def _one(self,sql:str,args:tuple=()):
        with self._connect() as db:
            row=db.execute(sql,args).fetchone(); return dict(row) if row else None

    def migrate_legacy(self):
        with self.transaction(True) as db:
            if db.execute("SELECT 1 FROM schema_metadata WHERE key='legacy_migration_v1'").fetchone():return
            leads_path=self.data_dir/"leads.json"; results=self.data_dir/"call_results"
            if leads_path.exists():
                try: rows=json.loads(leads_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError,OSError): rows=[]
                for lead in rows if isinstance(rows,list) else []:
                    try: phone=self.normalize_phone(lead.get("phone_number")); name=self.clean_text(lead.get("business_name", ""),200)
                    except ValueError: continue
                    now=lead.get("created_at") or utcnow(); db.execute("INSERT OR IGNORE INTO leads(lead_id,business_name,phone_number,category,notes,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(lead.get("lead_id") or str(uuid4()),name,phone,str(lead.get("category") or "")[:100],str(lead.get("notes") or "")[:1000],lead.get("status") or "new",now,lead.get("updated_at") or now))
            if results.exists():
                for path in results.glob("*.json"):
                    try:r=json.loads(path.read_text(encoding="utf-8")); cid=r.get("call_id") or path.stem; phone=self.normalize_phone(r.get("phone") or r.get("phone_number"))
                    except (ValueError,json.JSONDecodeError,OSError):continue
                    structured=r.get("structured_response") or {}; ts=r.get("timestamp") or utcnow(); db.execute("INSERT OR IGNORE INTO calls(call_id,lead_id,phone_number,business_name,category,notes,lifecycle_state,outcome,transcript,structured_response,interested,callback_requested,extraction_status,started_at,ended_at,duration,finalized_at,created_at,updated_at) VALUES(?,?,?,?,?,?,'COMPLETED',?,?,?,?,?,'succeeded',?,?,?,?,?,?)",(cid,r.get("lead_id"),phone,r.get("business_name") or "",r.get("category") or "",r.get("notes") or "",r.get("call_status") or "completed",r.get("transcript") or "",json.dumps(structured,ensure_ascii=False),bool(r.get("interested")),bool(r.get("callback_requested")),ts,ts,int(r.get("duration") or 0),ts,ts,ts))
            db.execute("INSERT INTO schema_metadata(key,value) VALUES('legacy_migration_v1',?)",(utcnow(),))

    async def aget_call(self,*a,**k):return await asyncio.to_thread(self.get_call,*a,**k)
    async def aenqueue_call(self,*a,**k):return await asyncio.to_thread(self.enqueue_call,*a,**k)
    async def aprovider_status(self,*a,**k):return await asyncio.to_thread(self.provider_status,*a,**k)
    async def aclaim_media(self,*a,**k):return await asyncio.to_thread(self.claim_media,*a,**k)
