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

from app.storage.json_store import EXPORT_HEADERS, JsonCallStore, Workbook

PROVIDER_TERMINAL = frozenset({"completed", "failed", "busy", "no-answer", "canceled"})
LIFECYCLE_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELED", "BUSY", "NO_ANSWER", "HUNG_UP"})
CAPACITY_STATES = ("claimed", "active", "canceling")
UNRESOLVED_JOB_STATES = ("queued", "claimed", "active", "canceling", "needs_reconciliation")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class SuppressedError(RuntimeError):
    pass


class ActiveDataError(RuntimeError):
    """Raised when an operator tries to delete an in-progress call."""


class SQLiteCallStore(JsonCallStore):
    """Multi-process SQLite repository using one connection per operation."""

    def __init__(self, database_path: Path, legacy_data_dir: Path | None = None) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(legacy_data_dir or self.database_path.parent)
        self._initialize_schema()
        self.migrate_legacy()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        with self.transaction(immediate=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS schema_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS leads(
                lead_id TEXT PRIMARY KEY,business_name TEXT NOT NULL,phone_number TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'new',
                do_not_call INTEGER NOT NULL DEFAULT 0,review_required INTEGER NOT NULL DEFAULT 0,
                last_call_id TEXT,last_call_sid TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS calls(
                call_id TEXT PRIMARY KEY,lead_id TEXT REFERENCES leads(lead_id),phone_number TEXT NOT NULL,
                business_name TEXT NOT NULL DEFAULT '',category TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',
                call_sid TEXT UNIQUE,lifecycle_state TEXT NOT NULL,provider_status TEXT,outcome TEXT,
                media_connected INTEGER NOT NULL DEFAULT 0,media_owner TEXT,transcript TEXT NOT NULL DEFAULT '',
                structured_response TEXT NOT NULL DEFAULT '{}',interested INTEGER NOT NULL DEFAULT 0,
                callback_requested INTEGER NOT NULL DEFAULT 0,do_not_call_requested INTEGER NOT NULL DEFAULT 0,
                extraction_status TEXT NOT NULL DEFAULT 'not_required',extraction_error TEXT,
                extraction_attempts INTEGER NOT NULL DEFAULT 0,started_at TEXT,ended_at TEXT,duration INTEGER NOT NULL DEFAULT 0,
                finalized_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS call_events(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,call_id TEXT NOT NULL REFERENCES calls(call_id),
                event_name TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT '{}',timestamp TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS call_jobs(
                job_id TEXT PRIMARY KEY,call_id TEXT NOT NULL UNIQUE REFERENCES calls(call_id),queue_state TEXT NOT NULL,
                lease_owner TEXT,lease_expires_at TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,idempotency_key TEXT UNIQUE,
                created_at TEXT NOT NULL,claimed_at TEXT,updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS suppression_list(
                phone_number TEXT PRIMARY KEY,source_call_id TEXT REFERENCES calls(call_id),reason TEXT NOT NULL,created_at TEXT NOT NULL)""")
            self._migrate_schema_v2(db)

    def _migrate_schema_v2(self, db: sqlite3.Connection) -> None:
        call_columns = {
            "media_end_reason": "TEXT", "raw_persisted_at": "TEXT", "media_ended_at": "TEXT",
            "provider_terminal_at": "TEXT", "ring_deadline": "TEXT", "max_call_deadline": "TEXT",
            "reconciliation_status": "TEXT", "reconciliation_error": "TEXT",
            "reconciliation_attempts": "INTEGER NOT NULL DEFAULT 0", "next_reconciliation_at": "TEXT",
            "action_owner": "TEXT", "action_lease_expires_at": "TEXT",
            # Twilio's async AMD verdict, kept separate from provider_status
            # and outcome so a machine pickup stays distinguishable from a
            # human who hung up early.
            "answered_by": "TEXT", "answered_by_at": "TEXT",
        }
        existing = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
        for name, declaration in call_columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE calls ADD COLUMN {name} {declaration}")
        db.execute("""CREATE TABLE IF NOT EXISTS extraction_jobs(
            call_id TEXT PRIMARY KEY REFERENCES calls(call_id),state TEXT NOT NULL,lease_owner TEXT,
            lease_expires_at TEXT,next_attempt_at TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
        # Rebuild the old index: reconciliation is intentionally unresolved and blocks this phone.
        db.execute("DROP INDEX IF EXISTS one_active_phone")
        terminal = "'COMPLETED','FAILED','CANCELED','BUSY','NO_ANSWER','HUNG_UP'"
        db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS one_active_phone ON calls(phone_number) WHERE lifecycle_state NOT IN ({terminal})")
        db.execute("CREATE INDEX IF NOT EXISTS jobs_claim ON call_jobs(queue_state,lease_expires_at,created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS calls_deadlines ON calls(provider_terminal_at,ring_deadline,max_call_deadline)")
        db.execute("CREATE INDEX IF NOT EXISTS calls_reconcile ON calls(reconciliation_status,next_reconciliation_at)")
        db.execute("CREATE INDEX IF NOT EXISTS extraction_claim ON extraction_jobs(state,next_attempt_at,lease_expires_at)")
        db.execute("INSERT OR REPLACE INTO schema_metadata(key,value) VALUES('schema_version','2')")

    @staticmethod
    def normalize_phone(value: Any) -> str:
        if not isinstance(value, (str, int, float)):
            raise ValueError("Phone number must be a scalar string")
        raw = str(value).strip()
        digits = re.sub(r"\D", "", raw)
        if not raw:
            raise ValueError("Phone number is required")
        if not raw.startswith("+") and len(digits) == 10:
            digits = "91" + digits
        if not 8 <= len(digits) <= 15:
            raise ValueError("Phone number must be E.164 compatible")
        return "+" + digits

    @staticmethod
    def clean_text(value: Any, maximum: int) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("Lead fields must be strings")
        return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value)).strip()[:maximum]

    def validate_lead(self, row: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
        try:
            normalized = {
                "business_name": self.clean_text(row.get("business_name"), 200),
                "phone_number": self.normalize_phone(row.get("phone_number")),
                "category": self.clean_text(row.get("category"), 100),
                "notes": self.clean_text(row.get("notes"), 1000),
            }
        except ValueError as error:
            return {"business_name": "", "phone_number": "", "category": "", "notes": ""}, [str(error)]
        errors = [] if normalized["business_name"] else ["Missing business name"]
        return normalized, errors

    def import_leads(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        imported, rejected = [], []
        duplicates = 0
        with self.transaction(immediate=True) as db:
            for index, row in enumerate(rows, 1):
                clean, errors = self.validate_lead(row)
                if errors:
                    rejected.append({"row": index, "errors": errors, "lead": row})
                    continue
                now = utcnow()
                lead = {**clean, "lead_id": str(uuid4()), "status": "new", "created_at": now, "updated_at": now}
                try:
                    db.execute("""INSERT INTO leads(lead_id,business_name,phone_number,category,notes,status,created_at,updated_at)
                        VALUES(:lead_id,:business_name,:phone_number,:category,:notes,:status,:created_at,:updated_at)""", lead)
                    imported.append(lead)
                except sqlite3.IntegrityError:
                    duplicates += 1
                    rejected.append({"row": index, "errors": ["Duplicate phone number"], "lead": clean})
        return {"imported": len(imported), "duplicates": duplicates, "rejected": rejected, "total_submitted": len(rows), "leads": imported}

    def list_leads(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM leads ORDER BY created_at DESC")

    def get_lead(self, lead_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM leads WHERE lead_id=?", (lead_id,))

    def update_lead(self, lead_id: str, **updates: Any) -> dict[str, Any] | None:
        allowed = {"business_name", "phone_number", "category", "notes", "status", "do_not_call", "review_required", "last_call_id", "last_call_sid", "last_error"}
        values = {key: value for key, value in updates.items() if key in allowed and value is not None}
        if not values:
            return self.get_lead(lead_id)
        values.update(updated_at=utcnow(), lead_id=lead_id)
        assignments = ",".join(f"{key}=:{key}" for key in values if key != "lead_id")
        with self.transaction(immediate=True) as db:
            db.execute(f"UPDATE leads SET {assignments} WHERE lead_id=:lead_id", values)
        return self.get_lead(lead_id)

    def delete_lead(self, lead_id: str) -> bool:
        """Delete a lead while retaining its calls as standalone history."""
        with self.transaction(immediate=True) as db:
            if not db.execute("SELECT 1 FROM leads WHERE lead_id=?", (lead_id,)).fetchone():
                return False
            db.execute("UPDATE calls SET lead_id=NULL WHERE lead_id=?", (lead_id,))
            return db.execute("DELETE FROM leads WHERE lead_id=?", (lead_id,)).rowcount == 1

    @staticmethod
    def _delete_call_db(db: sqlite3.Connection, call_id: str) -> None:
        db.execute("UPDATE suppression_list SET source_call_id=NULL WHERE source_call_id=?", (call_id,))
        db.execute("DELETE FROM extraction_jobs WHERE call_id=?", (call_id,))
        db.execute("DELETE FROM call_events WHERE call_id=?", (call_id,))
        db.execute("DELETE FROM call_jobs WHERE call_id=?", (call_id,))
        db.execute("DELETE FROM calls WHERE call_id=?", (call_id,))

    def delete_call(self, call_id: str) -> bool:
        with self.transaction(immediate=True) as db:
            row = db.execute("SELECT lifecycle_state FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if not row:
                return False
            if row[0] not in LIFECYCLE_TERMINAL:
                raise ActiveDataError("Active or unresolved calls cannot be deleted")
            self._delete_call_db(db, call_id)
            return True

    def clear_data(self, scope: str) -> dict[str, int]:
        """Delete operator data atomically; active calls are always protected."""
        if scope not in {"leads", "calls", "all"}:
            raise ValueError("scope must be leads, calls, or all")
        with self.transaction(immediate=True) as db:
            deleted = {"leads": 0, "calls": 0, "suppression_entries": 0}
            if scope in {"calls", "all"}:
                active = db.execute(
                    f"SELECT COUNT(*) FROM calls WHERE lifecycle_state NOT IN ({','.join('?' for _ in LIFECYCLE_TERMINAL)})",
                    tuple(LIFECYCLE_TERMINAL),
                ).fetchone()[0]
                if active:
                    raise ActiveDataError(f"End or resolve {active} active call(s) before deleting call data")
                call_ids = [row[0] for row in db.execute("SELECT call_id FROM calls").fetchall()]
                for call_id in call_ids:
                    self._delete_call_db(db, call_id)
                deleted["calls"] = len(call_ids)
            if scope in {"leads", "all"}:
                deleted["leads"] = db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
                db.execute("UPDATE calls SET lead_id=NULL")
                db.execute("DELETE FROM leads")
            if scope == "all":
                deleted["suppression_entries"] = db.execute("SELECT COUNT(*) FROM suppression_list").fetchone()[0]
                db.execute("DELETE FROM suppression_list")
            return deleted

    def enqueue_call(self, *, phone_number: str, lead_id: str | None = None, business_name: str = "", category: str = "", notes: str = "", idempotency_key: str | None = None) -> dict[str, Any]:
        phone = self.normalize_phone(phone_number)
        now, call_id = utcnow(), str(uuid4())
        with self.transaction(immediate=True) as db:
            if idempotency_key:
                old = db.execute("SELECT call_id FROM call_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if old:
                    return self._call_from_db(db, old[0])
            if db.execute("SELECT 1 FROM suppression_list WHERE phone_number=?", (phone,)).fetchone():
                raise SuppressedError("Phone is on the do-not-call list")
            if lead_id:
                lead = db.execute("SELECT do_not_call,review_required FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
                if not lead:
                    raise KeyError("Lead not found")
                if lead[0] or lead[1]:
                    raise SuppressedError("Lead is not callable")
            unresolved = db.execute("SELECT call_id FROM call_jobs j JOIN calls c USING(call_id) WHERE c.phone_number=? AND j.queue_state IN (?,?,?,?,?) LIMIT 1", (phone, *UNRESOLVED_JOB_STATES)).fetchone()
            if unresolved:
                return self._call_from_db(db, unresolved[0])
            db.execute("""INSERT INTO calls(call_id,lead_id,phone_number,business_name,category,notes,lifecycle_state,extraction_status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'QUEUED','not_required',?,?)""", (call_id, lead_id, phone, self.clean_text(business_name, 200), self.clean_text(category, 100), self.clean_text(notes, 1000), now, now))
            db.execute("INSERT INTO call_jobs(job_id,call_id,queue_state,idempotency_key,created_at,updated_at) VALUES(?,?,'queued',?,?,?)", (call_id, call_id, idempotency_key, now, now))
            self._event(db, call_id, "queued", now)
            if lead_id:
                db.execute("UPDATE leads SET status='queued',last_call_id=?,updated_at=? WHERE lead_id=?", (call_id, now, lead_id))
            return self._call_from_db(db, call_id)

    def claim_job(self, owner: str, limit: int, lease_seconds: int = 30) -> dict[str, Any] | None:
        now, expiry = utcnow(), future(lease_seconds)
        with self.transaction(immediate=True) as db:
            # An expired dial lease is ambiguous: quarantine it rather than redialing.
            expired = db.execute("SELECT call_id FROM call_jobs WHERE queue_state='claimed' AND lease_expires_at<?", (now,)).fetchall()
            for row in expired:
                db.execute("""UPDATE call_jobs SET queue_state='needs_reconciliation',lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                    WHERE call_id=? AND queue_state='claimed' AND lease_expires_at<?""", (now, row[0], now))
                db.execute("""UPDATE calls SET lifecycle_state='NEEDS_RECONCILIATION',reconciliation_status='dial_ambiguous',
                    next_reconciliation_at=?,updated_at=? WHERE call_id=? AND call_sid IS NULL""", (now, now, row[0]))
                self._event(db, row[0], "dial_lease_expired", now)
            active = db.execute("SELECT COUNT(*) FROM call_jobs WHERE queue_state IN ('claimed','active','canceling')").fetchone()[0]
            if active >= limit:
                return None
            candidate = db.execute("SELECT call_id FROM call_jobs WHERE queue_state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not candidate:
                return None
            changed = db.execute("""UPDATE call_jobs SET queue_state='claimed',lease_owner=?,lease_expires_at=?,
                claimed_at=COALESCE(claimed_at,?),attempt_count=attempt_count+1,updated_at=?
                WHERE call_id=? AND queue_state='queued'""", (owner, expiry, now, now, candidate[0])).rowcount
            if changed != 1:
                return None
            db.execute("UPDATE calls SET lifecycle_state='CONNECTING',started_at=COALESCE(started_at,?),updated_at=? WHERE call_id=?", (now, now, candidate[0]))
            return self._call_from_db(db, candidate[0])

    def bind_call_sid(self, call_id: str, sid: str, ring_seconds: int = 45, max_seconds: int = 900) -> bool:
        now = utcnow()
        with self.transaction(immediate=True) as db:
            changed = db.execute("""UPDATE calls SET call_sid=?,provider_status=COALESCE(provider_status,'initiated'),
                ring_deadline=?,max_call_deadline=?,reconciliation_status=NULL,reconciliation_error=NULL,updated_at=?
                WHERE call_id=? AND (call_sid IS NULL OR call_sid=?) AND provider_terminal_at IS NULL""",
                (sid, future(ring_seconds), future(max_seconds), now, call_id, sid)).rowcount
            if changed != 1:
                self._mark_reconciliation_db(db, call_id, "sid_binding_conflict", "Provider call could not be correlated", now)
                return False
            db.execute("UPDATE call_jobs SET queue_state='active',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?", (now, call_id))
            self._event(db, call_id, "provider_bound", now, {"call_sid": sid})
            return True

    def relinquish_claim(self, call_id: str, owner: str) -> bool:
        """Return a never-submitted claim to the queue during orderly shutdown."""
        now = utcnow()
        with self.transaction(immediate=True) as db:
            changed = db.execute("""UPDATE call_jobs SET queue_state='queued',lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                WHERE call_id=? AND queue_state='claimed' AND lease_owner=?""", (now, call_id, owner)).rowcount
            if changed:
                db.execute("UPDATE calls SET lifecycle_state='QUEUED',updated_at=? WHERE call_id=? AND call_sid IS NULL", (now, call_id))
            return changed == 1

    def mark_dial_ambiguous(self, call_id: str, category: str = "dial_ambiguous") -> None:
        with self.transaction(immediate=True) as db:
            self._mark_reconciliation_db(db, call_id, category, "Provider acceptance is unknown", utcnow())

    def mark_dial_rejected(self, call_id: str, category: str) -> None:
        now = utcnow()
        with self.transaction(immediate=True) as db:
            db.execute("UPDATE calls SET lifecycle_state='FAILED',outcome='provider_rejected',reconciliation_error=?,provider_terminal_at=?,finalized_at=?,updated_at=? WHERE call_id=?", (category[:200], now, now, now, call_id))
            db.execute("UPDATE call_jobs SET queue_state='terminal',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?", (now, call_id))
            self._event(db, call_id, "dial_rejected", now, {"category": category[:100]})

    def _mark_reconciliation_db(self, db: sqlite3.Connection, call_id: str, status: str, error: str, now: str) -> None:
        db.execute("UPDATE calls SET lifecycle_state='NEEDS_RECONCILIATION',reconciliation_status=?,reconciliation_error=?,next_reconciliation_at=?,updated_at=? WHERE call_id=? AND provider_terminal_at IS NULL", (status, error[:500], now, now, call_id))
        db.execute("UPDATE call_jobs SET queue_state='needs_reconciliation',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?", (now, call_id))
        self._event(db, call_id, status, now)

    def claim_media(self, call_id: str, sid: str, owner: str) -> dict[str, Any] | None:
        now = utcnow()
        with self.transaction(immediate=True) as db:
            changed = db.execute("""UPDATE calls SET media_owner=?,media_connected=1,lifecycle_state='CONNECTED',
                provider_status=CASE WHEN provider_status IS NULL THEN 'in-progress' ELSE provider_status END,updated_at=?
                WHERE call_id=? AND call_sid=? AND media_owner IS NULL AND provider_terminal_at IS NULL""", (owner, now, call_id, sid)).rowcount
            if changed != 1:
                return None
            self._event(db, call_id, "media_connected", now)
            return self._call_from_db(db, call_id)

    def provider_status(self, call_id: str, status: str, sid: str | None = None) -> dict[str, Any] | None:
        status, now = status.lower(), utcnow()
        with self.transaction(immediate=True) as db:
            row = db.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if not row or (sid and row["call_sid"] not in (None, sid)):
                return None
            if sid and row["call_sid"] is None:
                db.execute("UPDATE calls SET call_sid=?,ring_deadline=COALESCE(ring_deadline,?),max_call_deadline=COALESCE(max_call_deadline,?) WHERE call_id=?", (sid, future(45), future(900), call_id))
                db.execute("UPDATE call_jobs SET queue_state='active',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?", (now, call_id))
            if status in PROVIDER_TERMINAL:
                lifecycle = {"busy": "BUSY", "no-answer": "NO_ANSWER", "failed": "FAILED", "canceled": "CANCELED", "completed": "COMPLETED"}[status]
                db.execute("""UPDATE calls SET provider_status=?,provider_terminal_at=COALESCE(provider_terminal_at,?),
                    lifecycle_state=CASE WHEN provider_terminal_at IS NOT NULL THEN lifecycle_state
                        WHEN ?='COMPLETED' AND media_end_reason IN ('error','ai_disconnected') THEN 'FAILED'
                        WHEN ?='COMPLETED' AND media_end_reason IN ('client_disconnected','hung_up') THEN 'HUNG_UP' ELSE ? END,
                    outcome=CASE WHEN provider_terminal_at IS NOT NULL THEN outcome
                        WHEN ?='completed' AND media_end_reason IS NOT NULL AND media_end_reason!='completed' THEN media_end_reason ELSE ? END,
                    ended_at=COALESCE(ended_at,?),finalized_at=CASE WHEN media_connected=0 OR raw_persisted_at IS NOT NULL THEN COALESCE(finalized_at,?) ELSE finalized_at END,
                    reconciliation_status=NULL,reconciliation_error=NULL,updated_at=? WHERE call_id=?""",
                    (status, now, lifecycle, lifecycle, lifecycle, status, status, now, now, now, call_id))
                db.execute("UPDATE call_jobs SET queue_state='terminal',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?", (now, call_id))
                if row["lead_id"]:
                    db.execute("UPDATE leads SET status=?,last_call_id=?,last_call_sid=COALESCE(?,last_call_sid),updated_at=? WHERE lead_id=?", (status, call_id, sid, now, row["lead_id"]))
            elif row["provider_terminal_at"] is None:
                db.execute("UPDATE calls SET provider_status=?,updated_at=? WHERE call_id=?", (status, now, call_id))
            self._event(db, call_id, "provider_" + status, now, {"status": status})
            return self._call_from_db(db, call_id)

    def persist_raw(self, session: Any, media_end_reason: str = "completed", max_extraction_attempts: int = 3) -> dict[str, Any]:
        now = utcnow()
        metadata = session.metadata or {}
        lifecycle = "AI_FINISHED" if media_end_reason == "completed" else "HUNG_UP" if media_end_reason in {"client_disconnected", "hung_up"} else "FAILED"
        with self.transaction(immediate=True) as db:
            lead_id = metadata.get("lead_id")
            if lead_id and not db.execute("SELECT 1 FROM leads WHERE lead_id=?", (lead_id,)).fetchone():
                lead_id = None
            db.execute("""INSERT OR IGNORE INTO calls(call_id,lead_id,phone_number,business_name,category,notes,lifecycle_state,media_connected,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'CREATED',?,?,?)""", (session.call_id, lead_id, session.phone_number or metadata.get("phone_number") or "browser", metadata.get("business_name") or "", metadata.get("category") or "", metadata.get("notes") or "", int(bool(metadata.get("media_connected", session.direction == "browser"))), session.started_at.isoformat(), now))
            db.execute("""UPDATE calls SET transcript=?,ended_at=COALESCE(ended_at,?),duration=?,media_connected=CASE WHEN ? THEN 1 ELSE media_connected END,
                media_end_reason=?,media_ended_at=COALESCE(media_ended_at,?),
                raw_persisted_at=COALESCE(raw_persisted_at,?),lifecycle_state=CASE WHEN provider_terminal_at IS NULL THEN ? ELSE lifecycle_state END,
                extraction_status=CASE WHEN (media_connected=1 OR ?) AND extraction_status='not_required' THEN 'pending' ELSE extraction_status END,
                finalized_at=CASE WHEN provider_terminal_at IS NOT NULL THEN COALESCE(finalized_at,?) ELSE finalized_at END,updated_at=? WHERE call_id=?""",
                (session.transcript_text, (session.ended_at or datetime.now(timezone.utc)).isoformat(), session.duration_seconds,
                 int(bool(metadata.get("media_connected", session.direction == "browser"))), media_end_reason, now, now,
                 lifecycle, int(bool(metadata.get("media_connected", session.direction == "browser"))),
                 now, now, session.call_id))
            connected = db.execute("SELECT media_connected FROM calls WHERE call_id=?", (session.call_id,)).fetchone()[0]
            if connected:
                db.execute("""INSERT INTO extraction_jobs(call_id,state,next_attempt_at,max_attempts,created_at,updated_at)
                    VALUES(?,'pending',?,?,?,?) ON CONFLICT(call_id) DO NOTHING""", (session.call_id, now, max_extraction_attempts, now, now))
            self._event(db, session.call_id, "raw_persisted", now, {"media_end_reason": media_end_reason})
            return self._call_from_db(db, session.call_id)

    def claim_extraction(self, owner: str, lease_seconds: int) -> dict[str, Any] | None:
        now, expiry = utcnow(), future(lease_seconds)
        with self.transaction(immediate=True) as db:
            candidate = db.execute("""SELECT call_id FROM extraction_jobs WHERE
                (state IN ('pending','retry_pending') AND next_attempt_at<=?) OR (state='running' AND lease_expires_at<?)
                ORDER BY next_attempt_at LIMIT 1""", (now, now)).fetchone()
            if not candidate:
                return None
            changed = db.execute("""UPDATE extraction_jobs SET state='running',lease_owner=?,lease_expires_at=?,updated_at=?
                WHERE call_id=? AND ((state IN ('pending','retry_pending') AND next_attempt_at<=?) OR (state='running' AND lease_expires_at<?))""",
                (owner, expiry, now, candidate[0], now, now)).rowcount
            if changed != 1:
                return None
            db.execute("UPDATE calls SET extraction_status='running',updated_at=? WHERE call_id=?", (now, candidate[0]))
            call = self._call_from_db(db, candidate[0])
            job = dict(db.execute("SELECT * FROM extraction_jobs WHERE call_id=?", (candidate[0],)).fetchone())
            call["extraction_job"] = job
            return call

    def complete_extraction(self, call_id: str, owner: str, answers: dict[str, Any]) -> bool:
        now = utcnow()
        interested = answers.get("account_creation_interest") == "yes"
        callback = answers.get("callback_approved") == "yes"
        dnd = answers.get("do_not_call_requested") == "yes"
        with self.transaction(immediate=True) as db:
            job = db.execute("SELECT * FROM extraction_jobs WHERE call_id=? AND state='running' AND lease_owner=?", (call_id, owner)).fetchone()
            if not job:
                return False
            call = db.execute("SELECT lead_id,phone_number FROM calls WHERE call_id=?", (call_id,)).fetchone()
            db.execute("""UPDATE calls SET structured_response=?,interested=?,callback_requested=?,do_not_call_requested=?,
                extraction_status='succeeded',extraction_error=NULL,extraction_attempts=extraction_attempts+1,updated_at=? WHERE call_id=?""",
                (json.dumps(answers, ensure_ascii=False), interested, callback, dnd, now, call_id))
            db.execute("UPDATE extraction_jobs SET state='succeeded',lease_owner=NULL,lease_expires_at=NULL,attempt_count=attempt_count+1,updated_at=? WHERE call_id=?", (now, call_id))
            if dnd and call:
                db.execute("INSERT OR IGNORE INTO suppression_list(phone_number,source_call_id,reason,created_at) VALUES(?,?,?,?)", (call["phone_number"], call_id, "customer_request", now))
                if call["lead_id"]:
                    db.execute("UPDATE leads SET do_not_call=1,status='do_not_call',updated_at=? WHERE lead_id=?", (now, call["lead_id"]))
            return True

    def fail_extraction(self, call_id: str, owner: str, category: str, retry_delay: float) -> bool:
        now = utcnow()
        with self.transaction(immediate=True) as db:
            job = db.execute("SELECT * FROM extraction_jobs WHERE call_id=? AND state='running' AND lease_owner=?", (call_id, owner)).fetchone()
            if not job:
                return False
            attempts = job["attempt_count"] + 1
            permanent = attempts >= job["max_attempts"]
            state = "failed" if permanent else "retry_pending"
            delay = retry_delay * (2 ** max(0, attempts - 1))
            db.execute("""UPDATE extraction_jobs SET state=?,lease_owner=NULL,lease_expires_at=NULL,attempt_count=?,
                next_attempt_at=?,last_error=?,updated_at=? WHERE call_id=?""", (state, attempts, future(delay), category[:200], now, call_id))
            db.execute("UPDATE calls SET extraction_status=?,extraction_error=?,extraction_attempts=?,updated_at=? WHERE call_id=?", (state, category[:200], attempts, now, call_id))
            call = db.execute("SELECT lead_id,media_connected FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if permanent and call and call["lead_id"] and call["media_connected"]:
                db.execute("UPDATE leads SET review_required=1,status='review_required',last_error='Extraction requires operator review',updated_at=? WHERE lead_id=?", (now, call["lead_id"]))
            return True

    # Compatibility wrappers use the same durable extraction transaction.
    def save_extraction(self, call_id: str, answers: dict[str, Any]) -> None:
        owner = "compat-" + str(uuid4())
        with self.transaction(immediate=True) as db:
            db.execute("INSERT INTO extraction_jobs(call_id,state,lease_owner,lease_expires_at,next_attempt_at,max_attempts,created_at,updated_at) VALUES(?,'running',?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET state='running',lease_owner=excluded.lease_owner", (call_id, owner, future(60), utcnow(), 1, utcnow(), utcnow()))
        self.complete_extraction(call_id, owner, answers)

    def extraction_failed(self, call_id: str, error: str, permanent: bool = False) -> None:
        owner = "compat-" + str(uuid4())
        with self.transaction(immediate=True) as db:
            db.execute("INSERT INTO extraction_jobs(call_id,state,lease_owner,lease_expires_at,next_attempt_at,max_attempts,created_at,updated_at) VALUES(?,'running',?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET state='running',lease_owner=excluded.lease_owner", (call_id, owner, future(60), utcnow(), 1 if permanent else 2, utcnow(), utcnow()))
        self.fail_extraction(call_id, owner, error, 0)

    def claim_due_action(self, owner: str, lease_seconds: int = 30) -> dict[str, Any] | None:
        now, expiry = utcnow(), future(lease_seconds)
        with self.transaction(immediate=True) as db:
            row = db.execute("""SELECT * FROM calls WHERE provider_terminal_at IS NULL AND call_sid IS NOT NULL
                AND (action_lease_expires_at IS NULL OR action_lease_expires_at<?)
                AND ((reconciliation_status IS NULL AND ((media_connected=0 AND ring_deadline<=?) OR max_call_deadline<=?))
                    OR (reconciliation_status IS NOT NULL AND next_reconciliation_at<=?))
                ORDER BY COALESCE(next_reconciliation_at,ring_deadline,max_call_deadline) LIMIT 1""", (now, now, now, now)).fetchone()
            if not row:
                return None
            changed = db.execute("UPDATE calls SET action_owner=?,action_lease_expires_at=?,reconciliation_status='checking_provider',updated_at=? WHERE call_id=? AND (action_lease_expires_at IS NULL OR action_lease_expires_at<?)", (owner, expiry, now, row["call_id"], now)).rowcount
            if changed != 1:
                return None
            db.execute("UPDATE call_jobs SET queue_state='canceling',updated_at=? WHERE call_id=?", (now, row["call_id"]))
            return self._call_from_db(db, row["call_id"])

    def reconciliation_result(self, call_id: str, owner: str, provider_status: str | None, error: str | None = None,
                              max_attempts: int = 8) -> None:
        """Record the outcome of one provider reconciliation attempt.

        A proven terminal status resolves the call normally. Anything else is
        retried with bounded backoff -- but only up to `max_attempts`, after
        which the job is quarantined.

        The bound exists because without it a call whose provider cannot be
        reached at all (network down, tunnel dead, credentials rotated)
        retries every 300 seconds forever while still occupying a capacity
        slot. At the default ceiling of one concurrent call that is a total
        outage of the queue, caused by a single call nobody can resolve.

        Quarantine does not invent a terminal status and does not redial.
        `needs_reconciliation` is the state this design already uses for an
        expired dial claim: unresolved, operator-visible, still blocking its
        phone number, and deliberately outside the capacity count. The
        invariant that capacity is never released by a *timer alone* holds --
        release here requires repeated, recorded, failed attempts to obtain
        proof, which is evidence, not elapsed time.
        """
        if provider_status:
            self.provider_status(call_id, provider_status)
        now = utcnow()
        with self.transaction(immediate=True) as db:
            row = db.execute("SELECT reconciliation_attempts,provider_terminal_at FROM calls WHERE call_id=? AND action_owner=?", (call_id, owner)).fetchone()
            if not row:
                return
            if row["provider_terminal_at"]:
                db.execute("UPDATE calls SET action_owner=NULL,action_lease_expires_at=NULL,reconciliation_status=NULL,reconciliation_error=NULL WHERE call_id=?", (call_id,))
                return
            attempts = row["reconciliation_attempts"] + 1
            reason = (error or "Provider remains nonterminal")[:500]
            if attempts >= max(1, max_attempts):
                db.execute("""UPDATE calls SET action_owner=NULL,action_lease_expires_at=NULL,
                    lifecycle_state=CASE WHEN provider_terminal_at IS NULL THEN 'NEEDS_RECONCILIATION' ELSE lifecycle_state END,
                    reconciliation_status='stalled',reconciliation_error=?,reconciliation_attempts=?,
                    next_reconciliation_at=NULL,updated_at=? WHERE call_id=?""", (reason, attempts, now, call_id))
                db.execute("UPDATE call_jobs SET queue_state='needs_reconciliation',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?", (now, call_id))
                self._event(db, call_id, "reconciliation_stalled", now, {"attempts": attempts})
                return
            db.execute("""UPDATE calls SET action_owner=NULL,action_lease_expires_at=NULL,reconciliation_status='retry_pending',
                reconciliation_error=?,reconciliation_attempts=?,next_reconciliation_at=?,updated_at=? WHERE call_id=?""",
                (reason, attempts, future(min(300, 5 * 2 ** min(attempts, 6))), now, call_id))
            db.execute("UPDATE call_jobs SET queue_state='active',updated_at=? WHERE call_id=?", (now, call_id))

    def quarantine_abandoned_jobs(self, grace_seconds: float = 300.0) -> int:
        """Release capacity held by calls that outlived every deadline.

        The deadline reconciler only picks up calls it can identify: it needs
        a CallSid and a due deadline. A call that never got either -- a dial
        whose webhooks never arrived, a coordinator killed between claiming
        and submitting -- holds its slot with nothing scheduled to ever look
        at it again.

        `grace_seconds` is added on top of the call's own maximum-call
        deadline, so this can only fire well after the call could still have
        been live. Like every other quarantine here it neither terminalizes
        nor redials; it makes the call an operator's problem instead of the
        queue's.
        """
        now, cutoff = utcnow(), future(-grace_seconds)
        with self.transaction(immediate=True) as db:
            # Deliberately keyed on the call's own deadline and nothing else.
            # An earlier version also required the row to be untouched
            # recently, which meant a call being actively (and fruitlessly)
            # retried refreshed `updated_at` on every attempt and was never
            # swept -- the exact case this exists for.
            stalled = db.execute("""SELECT c.call_id FROM calls c JOIN call_jobs j USING(call_id)
                WHERE j.queue_state IN ('claimed','active','canceling')
                  AND c.provider_terminal_at IS NULL
                  AND COALESCE(c.max_call_deadline, c.ring_deadline, c.created_at) < ?""", (cutoff,)).fetchall()
            for row in stalled:
                db.execute("""UPDATE calls SET lifecycle_state='NEEDS_RECONCILIATION',reconciliation_status='abandoned',
                    reconciliation_error='No provider resolution within the maximum call window',
                    next_reconciliation_at=NULL,action_owner=NULL,action_lease_expires_at=NULL,updated_at=?
                    WHERE call_id=? AND provider_terminal_at IS NULL""", (now, row[0]))
                db.execute("UPDATE call_jobs SET queue_state='needs_reconciliation',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?", (now, row[0]))
                self._event(db, row[0], "job_abandoned", now)
            return len(stalled)

    def resolve_stuck_call(self, call_id: str, status: str = "failed", note: str = "operator_resolved") -> dict[str, Any] | None:
        """Operator override for a call the provider will never resolve.

        Deliberately not automatic and deliberately not a timer. Someone has
        checked the provider console and is asserting the outcome; that is
        the proof the durable design requires before a call becomes terminal.
        The phone stays suppressed from redial by the usual rules, and the
        override is recorded as an event so the history shows a human decided
        rather than the system guessing.
        """
        if status not in PROVIDER_TERMINAL:
            raise ValueError(f"status must be one of {sorted(PROVIDER_TERMINAL)}")
        now = utcnow()
        with self.transaction(immediate=True) as db:
            row = db.execute("SELECT lifecycle_state,provider_terminal_at FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if not row:
                return None
            if row["provider_terminal_at"]:
                return self._call_from_db(db, call_id)
            self._event(db, call_id, "operator_resolved", now, {"status": status, "note": note[:120]})
        self.provider_status(call_id, status)
        with self.transaction(immediate=True) as db:
            db.execute("UPDATE calls SET reconciliation_status=NULL,reconciliation_error=NULL,next_reconciliation_at=NULL,action_owner=NULL,action_lease_expires_at=NULL,updated_at=? WHERE call_id=?", (now, call_id))
            db.execute("UPDATE call_jobs SET queue_state='terminal',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE call_id=?", (now, call_id))
            return self._call_from_db(db, call_id)

    def capacity_snapshot(self) -> dict[str, Any]:
        """What is currently occupying the queue, and for how long."""
        with self._connect() as db:
            states = dict(db.execute("SELECT queue_state,COUNT(*) FROM call_jobs GROUP BY queue_state"))
            occupied = sum(states.get(state, 0) for state in CAPACITY_STATES)
            oldest = db.execute("""SELECT c.call_id,c.lifecycle_state,c.provider_status,c.created_at,j.queue_state
                FROM calls c JOIN call_jobs j USING(call_id)
                WHERE j.queue_state IN ('claimed','active','canceling') ORDER BY c.created_at LIMIT 5""").fetchall()
        return {
            "queue_states": states,
            "capacity_occupied": occupied,
            "oldest_occupying": [dict(row) for row in oldest],
        }

    def list_reconciliation(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM calls WHERE reconciliation_status IS NOT NULL AND provider_terminal_at IS NULL ORDER BY updated_at LIMIT ?", (min(limit, 100),))

    def list_calls(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._rows("SELECT *,phone_number AS phone,outcome AS call_status,COALESCE(ended_at,started_at,created_at) AS timestamp FROM calls ORDER BY created_at DESC LIMIT ? OFFSET ?", (min(max(limit, 1), 500), max(offset, 0)))
        return [self._decode_call(row) for row in rows]

    def iter_calls(self, chunk_size: int = 500) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            rows = self._rows("SELECT *,phone_number AS phone,outcome AS call_status,COALESCE(ended_at,started_at,created_at) AS timestamp FROM calls ORDER BY created_at DESC LIMIT ? OFFSET ?", (chunk_size, offset))
            if not rows:
                break
            for row in rows:
                yield self._decode_call(row)
            offset += len(rows)

    def list_live_calls(self, limit: int = 100) -> list[dict[str, Any]]:
        terminal = ",".join("?" for _ in LIFECYCLE_TERMINAL)
        return self._rows(f"SELECT *,lifecycle_state AS status FROM calls WHERE lifecycle_state NOT IN ({terminal}) ORDER BY updated_at DESC LIMIT ?", (*LIFECYCLE_TERMINAL, min(limit, 100)))

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT *,phone_number AS phone,outcome AS call_status,COALESCE(ended_at,started_at,created_at) AS timestamp FROM calls WHERE call_id=?", (call_id,))
        return self._decode_call(row) if row else None

    def statistics(self) -> dict[str, Any]:
        with self._connect() as db:
            # Every aggregate is COALESCEd. SUM() over zero rows returns NULL,
            # not 0, so on a fresh database the arithmetic below raised a
            # TypeError and the dashboard's stats panel 500'd on first load --
            # before a single call had been placed.
            row = db.execute("""SELECT COUNT(*) total_calls,
                COALESCE(SUM(CASE WHEN media_connected=1 THEN 1 ELSE 0 END),0) calls_answered,
                COALESCE(SUM(CASE WHEN outcome IN ('busy','failed') THEN 1 ELSE 0 END),0) busy_failed_calls,
                COALESCE(AVG(duration),0) average_call_duration,
                COALESCE(SUM(interested),0) interested_responses,
                COALESCE(SUM(callback_requested),0) callback_requested FROM calls""").fetchone()
            outcomes = dict(db.execute("SELECT COALESCE(outcome,'unknown'),COUNT(*) FROM calls GROUP BY COALESCE(outcome,'unknown')"))
            days = dict(db.execute("SELECT substr(COALESCE(ended_at,started_at,created_at),1,10),COUNT(*) FROM calls GROUP BY 1"))
            leads = db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        result = dict(row)
        result["total_leads"] = leads
        result["outcomes"] = outcomes
        result["calls_over_time"] = days
        result["calls_not_answered"] = result["total_calls"] - result["calls_answered"]
        result["success_rate"] = round(100 * (result["interested_responses"] or 0) / result["total_calls"], 1) if result["total_calls"] else 0
        result["average_call_duration"] = round(result["average_call_duration"], 1)
        return result

    def export_calls(self, fmt: str) -> tuple[str, bytes, str]:
        if fmt == "json":
            calls = list(self.iter_calls())
            return "call_results.json", json.dumps(calls, ensure_ascii=False, indent=2).encode(), "application/json"
        if fmt == "csv":
            output = StringIO()
            writer = csv.DictWriter(output, fieldnames=EXPORT_HEADERS)
            writer.writeheader()
            for call in self.iter_calls():
                writer.writerow(self._flat_call(call))
            return "call_results.csv", output.getvalue().encode(), "text/csv"
        if Workbook is None:
            raise RuntimeError("openpyxl is required for Excel export")
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Call Results")
        sheet.append(EXPORT_HEADERS)
        for call in self.iter_calls():
            row = self._flat_call(call)
            sheet.append([row.get(header, "") for header in EXPORT_HEADERS])
        output = BytesIO(); workbook.save(output); workbook.close()
        return "call_results.xlsx", output.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _decode_call(self, row: dict[str, Any]) -> dict[str, Any]:
        structured = row.get("structured_response") or "{}"
        if isinstance(structured, str):
            try:
                row["structured_response"] = json.loads(structured)
            except json.JSONDecodeError:
                row["structured_response"] = {}
        row["summary"] = self._summary(row["structured_response"])
        return row

    def _call_from_db(self, db: sqlite3.Connection, call_id: str) -> dict[str, Any]:
        row = db.execute("SELECT *,phone_number AS phone,outcome AS call_status FROM calls WHERE call_id=?", (call_id,)).fetchone()
        if not row:
            raise KeyError(call_id)
        return self._decode_call(dict(row))

    def record_answered_by(self, call_id: str, answered_by: str, sid: str | None = None) -> dict[str, Any] | None:
        """Persist Twilio's async AMD verdict for a call.

        Deliberately records the verdict and nothing else. It does not
        terminalize the call, does not set an outcome, and does not release
        the job slot: AMD is a provider opinion delivered mid-call, not a
        proven terminal status, and capacity is only ever freed by one. The
        caller asks Twilio to complete the call; the resulting `completed`
        webhook frees the slot through the normal path.
        """
        answered_by, now = str(answered_by or "").lower()[:40], utcnow()
        if not answered_by:
            return None
        with self.transaction(immediate=True) as db:
            row = db.execute("SELECT call_sid,answered_by FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if not row or (sid and row["call_sid"] not in (None, sid)):
                return None
            if row["answered_by"]:
                # AMD callbacks can be redelivered; the first verdict stands.
                return self._call_from_db(db, call_id)
            db.execute("UPDATE calls SET answered_by=?,answered_by_at=?,updated_at=? WHERE call_id=?", (answered_by, now, now, call_id))
            self._event(db, call_id, "answered_by", now, {"answered_by": answered_by})
            return self._call_from_db(db, call_id)

    def record_events(self, call_id: str, events: list[tuple[str, dict[str, Any]]]) -> int:
        """Bulk-append instrumentation events for one call.

        Written in a single transaction because the metrics writer batches:
        one commit per flush rather than one per measured turn keeps the
        instrumentation off the hot path of SQLite's write lock, which the
        coordinator and the provider webhooks are also contending for.

        A call row may already be gone (operator deletion mid-flush); the
        foreign key would then reject the batch. Metrics are never worth
        raising into a live call, so that is swallowed.
        """
        if not events:
            return 0
        now = utcnow()
        rows = [(call_id, name, json.dumps(metadata or {}, ensure_ascii=False), now) for name, metadata in events]
        try:
            with self.transaction(immediate=True) as db:
                db.executemany("INSERT INTO call_events(call_id,event_name,metadata,timestamp) VALUES(?,?,?,?)", rows)
        except sqlite3.Error:
            return 0
        return len(rows)

    def list_events(self, call_id: str, prefix: str = "") -> list[dict[str, Any]]:
        if prefix:
            return self._rows(
                "SELECT * FROM call_events WHERE call_id=? AND event_name LIKE ? ORDER BY event_id",
                (call_id, prefix + "%"),
            )
        return self._rows("SELECT * FROM call_events WHERE call_id=? ORDER BY event_id", (call_id,))

    def _event(self, db: sqlite3.Connection, call_id: str, name: str, timestamp: str, metadata: dict[str, Any] | None = None) -> None:
        # Exact duplicate callback events within one transaction are harmless; history remains bounded only by durable disk.
        db.execute("INSERT INTO call_events(call_id,event_name,metadata,timestamp) VALUES(?,?,?,?)", (call_id, name, json.dumps(metadata or {}), timestamp))

    def _rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def migrate_legacy(self) -> None:
        with self.transaction(immediate=True) as db:
            if not db.execute("SELECT 1 FROM schema_metadata WHERE key='legacy_migration_v1'").fetchone():
                self._migrate_legacy_records(db)
                db.execute("INSERT INTO schema_metadata(key,value) VALUES('legacy_migration_v1',?)", (utcnow(),))
            if not db.execute("SELECT 1 FROM schema_metadata WHERE key='legacy_dnd_v2'").fetchone():
                self._migrate_legacy_dnd(db)
                db.execute("INSERT INTO schema_metadata(key,value) VALUES('legacy_dnd_v2',?)", (utcnow(),))

    def _legacy_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return default

    def _migrate_legacy_records(self, db: sqlite3.Connection) -> None:
        leads = self._legacy_json(self.leads_path, [])
        for lead in leads if isinstance(leads, list) else []:
            try:
                phone = self.normalize_phone(lead.get("phone_number"))
                name = self.clean_text(lead.get("business_name", ""), 200)
                now = lead.get("created_at") or utcnow()
                dnd = bool(lead.get("do_not_call")) or lead.get("status") == "do_not_call"
                db.execute("""INSERT OR IGNORE INTO leads(lead_id,business_name,phone_number,category,notes,status,do_not_call,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (lead.get("lead_id") or str(uuid4()), name, phone, str(lead.get("category") or "")[:100], str(lead.get("notes") or "")[:1000], "do_not_call" if dnd else lead.get("status") or "new", dnd, now, lead.get("updated_at") or now))
            except (ValueError, sqlite3.Error, AttributeError):
                continue
        for path in self.results_dir.glob("*.json") if self.results_dir.exists() else []:
            record = self._legacy_json(path, None)
            if not isinstance(record, dict):
                continue
            try:
                phone = self.normalize_phone(record.get("phone") or record.get("phone_number"))
                structured = record.get("structured_response") or {}
                if isinstance(structured, str):
                    structured = json.loads(structured)
                if not isinstance(structured, dict):
                    structured = {}
                lead_id = record.get("lead_id")
                if lead_id and not db.execute("SELECT 1 FROM leads WHERE lead_id=?", (lead_id,)).fetchone():
                    lead_id = None
                timestamp = record.get("timestamp") or utcnow()
                db.execute("""INSERT OR IGNORE INTO calls(call_id,lead_id,phone_number,business_name,category,notes,lifecycle_state,outcome,transcript,
                    structured_response,interested,callback_requested,do_not_call_requested,extraction_status,started_at,ended_at,duration,
                    raw_persisted_at,provider_terminal_at,finalized_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'COMPLETED',?,?,?,?,?,?,'succeeded',?,?,?,?,?,?,?,?)""",
                    (record.get("call_id") or path.stem, lead_id, phone, record.get("business_name") or "", record.get("category") or "", record.get("notes") or "", record.get("call_status") or "completed", record.get("transcript") or "", json.dumps(structured, ensure_ascii=False), structured.get("account_creation_interest") == "yes" or bool(record.get("interested")), structured.get("callback_approved") == "yes" or bool(record.get("callback_requested")), structured.get("do_not_call_requested") == "yes", timestamp, timestamp, int(record.get("duration") or 0), timestamp, timestamp, timestamp, timestamp, timestamp))
            except (ValueError, sqlite3.Error, json.JSONDecodeError):
                continue

    def _migrate_legacy_dnd(self, db: sqlite3.Connection) -> None:
        now = utcnow()
        dnd_expression = "CASE WHEN json_valid(structured_response) THEN json_extract(structured_response,'$.do_not_call_requested') ELSE NULL END"
        db.execute(f"""INSERT OR IGNORE INTO suppression_list(phone_number,source_call_id,reason,created_at)
            SELECT phone_number,call_id,'legacy_customer_request',COALESCE(finalized_at,created_at) FROM calls
            WHERE do_not_call_requested=1 OR {dnd_expression}='yes'""")
        db.execute(f"UPDATE calls SET do_not_call_requested=1 WHERE {dnd_expression}='yes'")
        leads = self._legacy_json(self.leads_path, [])
        for lead in leads if isinstance(leads, list) else []:
            try:
                if bool(lead.get("do_not_call")) or lead.get("status") == "do_not_call":
                    phone = self.normalize_phone(lead.get("phone_number"))
                    db.execute("UPDATE leads SET do_not_call=1,status='do_not_call',updated_at=? WHERE phone_number=?", (now, phone))
                    db.execute("INSERT OR IGNORE INTO suppression_list(phone_number,source_call_id,reason,created_at) VALUES(?,NULL,'legacy_lead_flag',?)", (phone, now))
            except (AttributeError, ValueError):
                continue
        db.execute("""UPDATE leads SET do_not_call=1,status='do_not_call',updated_at=? WHERE
            do_not_call=1 OR status='do_not_call' OR phone_number IN (SELECT phone_number FROM suppression_list)""", (now,))
        db.execute("""INSERT OR IGNORE INTO suppression_list(phone_number,source_call_id,reason,created_at)
            SELECT phone_number,NULL,'legacy_lead_flag',? FROM leads WHERE do_not_call=1 OR status='do_not_call'""", (now,))

    async def aget_call(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_call, *args, **kwargs)

    async def aenqueue_call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.enqueue_call, *args, **kwargs)

    async def aprovider_status(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.provider_status, *args, **kwargs)

    async def aclaim_media(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.claim_media, *args, **kwargs)
