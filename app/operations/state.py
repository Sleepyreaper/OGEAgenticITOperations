"""Persistent finding workflow-state store (stdlib sqlite3, no external
dependency).

Findings themselves (app.operations.models.Finding) are re-derived fresh
on every collection run -- they carry no memory of "did a human already
look at this." This module is that memory: a small SQLite database,
keyed by Finding.id (deterministic -- see app.operations.identifiers), of
human triage state -- status, assigned owner, disposition reason, snooze
expiry -- plus a full audit trail of every transition.

Path is configurable via OperationsConfig.operations_state_db_path /
OPERATIONS_STATE_DB (default: a local, working-directory-relative file
suitable for local dev; on Azure App Service Linux, point this at
/home/data/operations.db -- /home is the only path persisted across
restarts/scale events on that platform).

Concurrency: every public method opens its own short-lived sqlite3
connection under a per-process `threading.RLock`, and every mutation runs
inside one explicit `BEGIN IMMEDIATE ... COMMIT` transaction (SQLite's
WAL journal mode + a 30s busy_timeout) -- safe for Flask's multi-threaded
request handling within one process, and for multiple Gunicorn worker
processes sharing the same on-disk file (SQLite's own file locking
serializes writers across processes; the in-process lock just avoids
unnecessary busy-wait retries within one worker).

Workflow status vocabulary here (new/acknowledged/in_progress/resolved/
dismissed/snoozed) is DELIBERATELY separate from
app.operations.models.FindingStatus (open/acknowledged/mitigating/
resolved/suppressed): the latter is the evidence's own platform-reported
state (set by a collector at Finding-construction time); this module's
workflow status is the human triage state of that Finding in the ops
tool, tracked independently of what the underlying alert/recommendation
itself reports.
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from app.operations.models import ensure_utc_iso, format_utc_iso, parse_utc_iso, utc_now

__all__ = [
    "WORKFLOW_STATUSES",
    "WORKFLOW_ACTIONS",
    "DEFAULT_WORKFLOW_STATUS",
    "OperationsStateError",
    "FindingStateRecord",
    "OperationsStateStore",
    "merge_workflow_state",
]

WORKFLOW_STATUSES = ("new", "acknowledged", "in_progress", "resolved", "dismissed", "snoozed")
DEFAULT_WORKFLOW_STATUS = "new"

# action -> {"allowed_from": {status, ...}, "to_status": str | None (None = status unchanged)}.
# `snooze` is deliberately only allowed from "new"/"acknowledged" (not
# "in_progress") so an expired snooze always has an unambiguous status to
# revert to -- "new" or "acknowledged", exactly the two states the
# product spec calls out.
_ACTION_RULES = {
    "acknowledge": {"allowed_from": {"new"}, "to_status": "acknowledged"},
    "start": {"allowed_from": {"new", "acknowledged"}, "to_status": "in_progress"},
    "resolve": {"allowed_from": {"new", "acknowledged", "in_progress", "snoozed"}, "to_status": "resolved"},
    "dismiss": {"allowed_from": {"new", "acknowledged", "in_progress", "snoozed"}, "to_status": "dismissed"},
    "snooze": {"allowed_from": {"new", "acknowledged"}, "to_status": "snoozed"},
    "assign": {"allowed_from": {"new", "acknowledged", "in_progress", "snoozed"}, "to_status": None},
}
WORKFLOW_ACTIONS = tuple(_ACTION_RULES.keys())

_MAX_SQL_IN_BATCH = 400  # stays well under SQLite's default 999-variable limit per statement


class OperationsStateError(ValueError):
    """Invalid workflow input or an illegal status transition -- e.g. an
    unrecognized action, `assign` with no owner, `snooze` with no/past
    `snooze_until`, or an action attempted from a status it doesn't
    allow (e.g. re-`acknowledge`-ing an already-acknowledged finding)."""


@dataclass
class FindingStateRecord:
    """One finding's persisted workflow state."""
    finding_id: str
    status: str
    assigned_owner: str = ""
    disposition_reason: str = ""
    snooze_until: Optional[str] = None
    pre_snooze_status: Optional[str] = None
    first_seen_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if self.status not in WORKFLOW_STATUSES:
            raise OperationsStateError(f"FindingStateRecord.status must be one of {WORKFLOW_STATUSES}, got {self.status!r}")

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "status": self.status,
            "assigned_owner": self.assigned_owner,
            "disposition_reason": self.disposition_reason,
            "snooze_until": self.snooze_until,
            "first_seen_at": self.first_seen_at or None,
            "created_at": self.created_at or None,
            "updated_at": self.updated_at or None,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS finding_state (
    finding_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    assigned_owner TEXT NOT NULL DEFAULT '',
    disposition_reason TEXT NOT NULL DEFAULT '',
    snooze_until TEXT,
    pre_snooze_status TEXT,
    first_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finding_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_finding_audit_finding_id ON finding_audit(finding_id, id DESC);
CREATE TABLE IF NOT EXISTS handoff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    open_finding_ids TEXT NOT NULL,
    summary_json TEXT NOT NULL
);
"""


class OperationsStateStore:
    def __init__(self, db_path: str):
        if not db_path or not db_path.strip():
            raise ValueError("db_path must not be blank")
        self.db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # In-process serialization for the read-modify-write sequences
        # below; SQLite's own WAL locking + busy_timeout (see _connect)
        # is what actually makes this safe across separate processes
        # (e.g. multiple Gunicorn workers sharing this file).
        self._lock = threading.RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
            finally:
                conn.close()

    @contextmanager
    def _transaction(self):
        """One atomic unit of work. `BEGIN IMMEDIATE` takes SQLite's
        write lock up front (rather than deferring it to the first write
        statement), so a read-then-write sequence inside this block can
        never race with another writer between the read and the write."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> FindingStateRecord:
        return FindingStateRecord(
            finding_id=row["finding_id"],
            status=row["status"],
            assigned_owner=row["assigned_owner"] or "",
            disposition_reason=row["disposition_reason"] or "",
            snooze_until=row["snooze_until"],
            pre_snooze_status=row["pre_snooze_status"],
            first_seen_at=row["first_seen_at"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def _maybe_expire_snooze_locked(self, conn: sqlite3.Connection, record: FindingStateRecord, *, now: datetime) -> FindingStateRecord:
        """If `record` is a snooze past its `snooze_until`, persist the
        auto-expiry (revert to `pre_snooze_status`, defaulting to 'new')
        plus an audit row, and return the updated record. A no-op
        (returns `record` unchanged) otherwise. Must be called with
        `conn` inside an already-open write transaction."""
        if record.status != "snoozed" or not record.snooze_until:
            return record
        if parse_utc_iso(record.snooze_until) > now:
            return record

        new_status = record.pre_snooze_status or DEFAULT_WORKFLOW_STATUS
        now_iso = format_utc_iso(now)
        conn.execute(
            "UPDATE finding_state SET status = ?, snooze_until = NULL, pre_snooze_status = NULL, updated_at = ? "
            "WHERE finding_id = ?",
            (new_status, now_iso, record.finding_id),
        )
        conn.execute(
            "INSERT INTO finding_audit (finding_id, action, from_status, to_status, actor, reason, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record.finding_id, "auto_unsnooze", "snoozed", new_status, "system", "snooze_until elapsed", now_iso),
        )
        record.status = new_status
        record.snooze_until = None
        record.pre_snooze_status = None
        record.updated_at = now_iso
        return record

    def get_state(self, finding_id: str, *, now: Optional[datetime] = None) -> FindingStateRecord:
        """The current workflow state for `finding_id`, auto-resolving an
        expired snooze first. A finding with no row yet is implicitly
        'new' -- this returns a synthetic (never persisted)
        FindingStateRecord for that case rather than None, so callers
        never need a separate "not tracked yet" branch."""
        now = now or utc_now()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM finding_state WHERE finding_id = ?", (finding_id,)).fetchone()
            if row is None:
                return FindingStateRecord(finding_id=finding_id, status=DEFAULT_WORKFLOW_STATUS)
            record = self._maybe_expire_snooze_locked(conn, self._row_to_record(row), now=now)
        return record

    def get_states(self, finding_ids: Iterable[str], *, now: Optional[datetime] = None) -> dict:
        """Batch form of get_state -- only returns entries that actually
        have a persisted row (callers should treat a missing key as
        'new', matching get_state's synthetic-default behavior)."""
        now = now or utc_now()
        unique_ids = [fid for fid in dict.fromkeys(finding_ids) if fid]
        if not unique_ids:
            return {}
        result = {}
        with self._transaction() as conn:
            for start in range(0, len(unique_ids), _MAX_SQL_IN_BATCH):
                chunk = unique_ids[start:start + _MAX_SQL_IN_BATCH]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM finding_state WHERE finding_id IN ({placeholders})", chunk
                ).fetchall()
                for row in rows:
                    record = self._maybe_expire_snooze_locked(conn, self._row_to_record(row), now=now)
                    result[record.finding_id] = record
        return result

    def apply_action(
        self,
        finding_id: str,
        action: str,
        *,
        actor: str,
        first_seen: Optional[str] = None,
        reason: str = "",
        owner: Optional[str] = None,
        snooze_until: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> FindingStateRecord:
        """Apply one workflow action to `finding_id`, validating the
        transition and required inputs, and recording an audit row.
        Raises OperationsStateError (never a silent no-op) on an unknown
        action, a disallowed transition, or a missing required field."""
        if not finding_id or not finding_id.strip():
            raise OperationsStateError("finding_id is required")
        if action not in _ACTION_RULES:
            raise OperationsStateError(f"unknown workflow action {action!r}; must be one of {sorted(_ACTION_RULES)}")
        if not actor or not actor.strip():
            raise OperationsStateError("actor is required")

        now = now or utc_now()
        now_iso = format_utc_iso(now)
        rule = _ACTION_RULES[action]

        if action == "assign" and (not owner or not owner.strip()):
            raise OperationsStateError("assign requires a non-empty owner")
        normalized_snooze_until = None
        if action == "snooze":
            if not snooze_until:
                raise OperationsStateError("snooze requires snooze_until")
            normalized_snooze_until = ensure_utc_iso(snooze_until, field_name="snooze_until")
            if parse_utc_iso(normalized_snooze_until) <= now:
                raise OperationsStateError("snooze_until must be in the future")

        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM finding_state WHERE finding_id = ?", (finding_id,)).fetchone()
            current = self._maybe_expire_snooze_locked(conn, self._row_to_record(row), now=now) if row is not None else None
            current_status = current.status if current is not None else DEFAULT_WORKFLOW_STATUS

            if current_status not in rule["allowed_from"]:
                raise OperationsStateError(
                    f"cannot {action!r} finding {finding_id!r} from status {current_status!r}; "
                    f"allowed from {sorted(rule['allowed_from'])}"
                )

            to_status = rule["to_status"] or current_status
            new_owner = owner.strip() if owner is not None else (current.assigned_owner if current is not None else "")
            new_reason = reason.strip() if reason else (current.disposition_reason if current is not None else "")
            new_pre_snooze_status = current_status if action == "snooze" else None
            new_snooze_until = normalized_snooze_until if action == "snooze" else None
            first_seen_value = (
                current.first_seen_at if current is not None
                else (ensure_utc_iso(first_seen, field_name="first_seen") if first_seen else now_iso)
            )
            created_at_value = current.created_at if current is not None else now_iso

            if current is None:
                conn.execute(
                    "INSERT INTO finding_state ("
                    "finding_id, status, assigned_owner, disposition_reason, snooze_until, pre_snooze_status, "
                    "first_seen_at, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (finding_id, to_status, new_owner, new_reason, new_snooze_until, new_pre_snooze_status,
                     first_seen_value, created_at_value, now_iso),
                )
            else:
                conn.execute(
                    "UPDATE finding_state SET status = ?, assigned_owner = ?, disposition_reason = ?, "
                    "snooze_until = ?, pre_snooze_status = ?, updated_at = ? WHERE finding_id = ?",
                    (to_status, new_owner, new_reason, new_snooze_until, new_pre_snooze_status, now_iso, finding_id),
                )
            conn.execute(
                "INSERT INTO finding_audit (finding_id, action, from_status, to_status, actor, reason, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (finding_id, action, current_status, to_status, actor.strip(), reason.strip() if reason else "", now_iso),
            )

        return FindingStateRecord(
            finding_id=finding_id, status=to_status, assigned_owner=new_owner, disposition_reason=new_reason,
            snooze_until=new_snooze_until, pre_snooze_status=new_pre_snooze_status,
            first_seen_at=first_seen_value, created_at=created_at_value, updated_at=now_iso,
        )

    def get_audit_history(self, finding_id: str, *, limit: int = 50) -> list:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT action, from_status, to_status, actor, reason, occurred_at FROM finding_audit "
                "WHERE finding_id = ? ORDER BY id DESC LIMIT ?",
                (finding_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_handoff(self, *, created_by: str, content_hash: str, open_finding_ids: list, summary: dict, now: Optional[datetime] = None) -> dict:
        """Persist a bounded handoff marker: timestamp, actor, an
        integrity hash of the computed handoff payload, and the bounded
        list of open finding IDs (ids only -- never raw evidence text or
        secrets) so a future handoff can compute "new/changed since"
        without re-deriving from a stored evidence dump."""
        now = now or utc_now()
        now_iso = format_utc_iso(now)
        bounded_ids = list(open_finding_ids)[:500]
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO handoff (created_at, created_by, content_hash, open_finding_ids, summary_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (now_iso, created_by or "", content_hash, json.dumps(bounded_ids), json.dumps(summary, default=str)),
            )
            handoff_id = cursor.lastrowid
        return {
            "id": handoff_id, "created_at": now_iso, "created_by": created_by or "", "content_hash": content_hash,
            "open_finding_ids": bounded_ids, "summary": summary,
        }

    @staticmethod
    def _handoff_row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "content_hash": row["content_hash"],
            "open_finding_ids": json.loads(row["open_finding_ids"]),
            "summary": json.loads(row["summary_json"]),
        }

    def get_latest_handoff(self) -> Optional[dict]:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM handoff ORDER BY id DESC LIMIT 1").fetchone()
        return self._handoff_row_to_dict(row) if row is not None else None

    def list_handoffs(self, *, limit: int = 20) -> list:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM handoff ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._handoff_row_to_dict(row) for row in rows]


def merge_workflow_state(findings: list, store: OperationsStateStore, *, now: Optional[datetime] = None) -> list:
    """Merge persisted workflow state onto a fresh list of Findings (by
    deterministic Finding.id), auto-resolving expired snoozes as a side
    effect. Returns one composite dict per finding:
    `{"finding": Finding.to_dict(), "workflow": {...}}` -- a finding with
    no persisted row yet gets the synthetic default ('new', no owner, no
    snooze) rather than being omitted.
    """
    now = now or utc_now()
    states = store.get_states([f.id for f in findings], now=now)
    merged = []
    for finding in findings:
        state = states.get(finding.id)
        if state is None:
            workflow = {
                "status": DEFAULT_WORKFLOW_STATUS, "assigned_owner": "", "disposition_reason": "",
                "snooze_until": None, "first_seen_at": None, "created_at": None, "updated_at": None,
            }
        else:
            workflow = state.to_dict()
            workflow.pop("finding_id", None)
        merged.append({"finding": finding.to_dict(), "workflow": workflow})
    return merged
