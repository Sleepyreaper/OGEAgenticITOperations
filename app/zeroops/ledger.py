"""Escalation ledger: every SRE Agent -> squad hand-off, with outcome and cost.

Stored in the same SQLite file as the operations state store
(``OPERATIONS_STATE_DB``) so a single App Service file share holds both.
"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from app.activity.store import ensure_activity_schema

STATUSES = ("received", "analyzing", "proposed", "approved", "rejected", "resolved", "failed")
SOURCES = ("sre-agent", "detector", "simulated", "api")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS zeroops_escalations (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL,
    incident_ref TEXT NOT NULL DEFAULT '',
    scenario_id TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL,
    sre_summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    routing_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    cost_json TEXT NOT NULL DEFAULT '{}',
    proposal_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    investigation_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_zeroops_escalations_created ON zeroops_escalations(created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EscalationLedger:
    def __init__(self, db_path: str):
        if not db_path or not db_path.strip():
            raise ValueError("db_path must not be blank")
        self.db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
            finally:
                conn.close()
        ensure_activity_schema(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in ("routing", "result", "cost"):
            item[key] = json.loads(item.pop(f"{key}_json") or "{}")
        return item

    def create(self, *, source: str, question: str, incident_ref: str = "", scenario_id: str = "",
               sre_summary: str = "", investigation_id: str = None) -> dict:
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        if not question or not question.strip():
            raise ValueError("question is required")
        if investigation_id is not None and (not isinstance(investigation_id, str) or not investigation_id.strip()):
            raise ValueError("investigation_id must be null or a non-empty string")
        now = _now()
        esc_id = "esc-" + uuid.uuid4().hex[:10]
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO zeroops_escalations (id, created_at, updated_at, source, incident_ref, scenario_id, "
                    "question, sre_summary, status, investigation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (esc_id, now, now, source, (incident_ref or "")[:200], (scenario_id or "")[:64],
                     question.strip()[:4000], (sre_summary or "")[:4000], "received",
                     investigation_id.strip()[:64] if investigation_id else None),
                )
            finally:
                conn.close()
        return self.get(esc_id)

    def update(self, esc_id: str, *, status: str = None, routing: dict = None, result: dict = None,
               cost: dict = None, proposal_id: str = None, error: str = None,
               investigation_id: str = None) -> dict:
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if investigation_id is not None and (not isinstance(investigation_id, str) or not investigation_id.strip()):
            raise ValueError("investigation_id must be null or a non-empty string")
        sets, values = ["updated_at = ?"], [_now()]
        for column, value in (("status", status), ("proposal_id", proposal_id), ("error", error)):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(value)
        for column, value in (("routing_json", routing), ("result_json", result), ("cost_json", cost)):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(json.dumps(value, default=str))
        if investigation_id is not None:
            sets.append("investigation_id = ?")
            values.append(investigation_id.strip()[:64])
        values.append(esc_id)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(f"UPDATE zeroops_escalations SET {', '.join(sets)} WHERE id = ?", values)
                if cur.rowcount == 0:
                    raise KeyError(esc_id)
            finally:
                conn.close()
        return self.get(esc_id)

    def get(self, esc_id: str):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM zeroops_escalations WHERE id = ?", (esc_id,)).fetchone()
        finally:
            conn.close()
        return self._row(row) if row else None

    def find_by_proposal(self, proposal_id: str):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM zeroops_escalations WHERE proposal_id = ?", (proposal_id,)).fetchone()
        finally:
            conn.close()
        return self._row(row) if row else None

    def list(self, limit: int = 50) -> list:
        limit = max(1, min(int(limit), 500))
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM zeroops_escalations ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [self._row(r) for r in rows]

    def summary(self) -> dict:
        items = self.list(limit=500)
        total_usd = sum(float((i.get("cost") or {}).get("total_estimated_usd") or 0) for i in items)
        total_tokens = sum(int(((i.get("cost") or {}).get("squad") or {}).get("total_tokens") or 0) for i in items)
        by_status, by_source = {}, {}
        for i in items:
            by_status[i["status"]] = by_status.get(i["status"], 0) + 1
            by_source[i["source"]] = by_source.get(i["source"], 0) + 1
        return {
            "count": len(items), "by_status": by_status, "by_source": by_source,
            "total_squad_tokens": total_tokens, "total_estimated_usd": round(total_usd, 4),
        }


_LEDGER = None
_LEDGER_LOCK = threading.Lock()


def get_ledger() -> EscalationLedger:
    global _LEDGER
    path = os.environ.get("OPERATIONS_STATE_DB", "").strip() or "operations_state.db"
    with _LEDGER_LOCK:
        if _LEDGER is None or _LEDGER.db_path != path:
            _LEDGER = EscalationLedger(path)
        return _LEDGER
