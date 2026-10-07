"""SQLite-backed Activity Proof state and sanitized public projections."""

import base64
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

INVESTIGATION_PHASES = (
    "opened",
    "in_progress",
    "analysis_ready",
    "needs_review",
    "approved",
    "rejected",
    "closed_unverified",
    "failed",
)
RUN_STATUSES = ("running", "completed", "insufficient_evidence", "invalid_output", "failed")
PROVENANCE = ("observed", "reported", "configured", "simulated", "approved", "executed", "verified")
WRITABLE_PROVENANCE = ("observed", "reported", "configured", "simulated", "approved")
ORIGINS = ("analysis", "briefing", "mcp", "detector", "simulated", "api", "sre_handoff")
TRIGGERS = ("analysis", "briefing", "escalation")

# Actors and receipt kinds are intentionally machine vocabularies, not display
# strings. New values require a contract change rather than being accepted as
# arbitrary caller input.
ACTORS = (
    "system",
    "operator",
    "detector",
    "sre_agent",
    "orchestrator",
    "specialist",
    "reviewer",
)
EVENT_KINDS = (
    "opened",
    "probe_result",
    "sre_thread_result",
    "escalation_received",
    "analysis_started",
    "model_completed",
    "tool_completed",
    "analysis_finished",
    "analysis_failed",
    "proposal_created",
    "decision_recorded",
)
ARTIFACT_KINDS = (
    "probe_receipt",
    "sre_handoff_receipt",
    "run_receipt",
    "proposal_receipt",
    "decision_receipt",
)

_LEGACY_EVENT_KINDS = (
    "investigation_opened",
    "phase_changed",
    "run_started",
    "backend_selected",
    "evidence_observed",
    "analysis_ready",
    "review_requested",
    "run_completed",
    "run_failed",
    "investigation_closed",
)
_LEGACY_ARTIFACT_KINDS = (
    "evidence_summary",
    "routing_summary",
    "analysis_summary",
    "recommendation",
    "review_summary",
    "approval_receipt",
    "execution_receipt",
    "verification_receipt",
)

MAX_PAYLOAD_BYTES = 16 * 1024
MAX_USAGE_BYTES = 8 * 1024
MAX_JSON_DEPTH = 5
MAX_CONTAINER_ITEMS = 100
MAX_STRING_LENGTH = 2000
MAX_PUBLIC_STRING_LENGTH = 256
MAX_NUMBER_ABS = 10**15

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SCENARIO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPAQUE_ID_RE = re.compile(r"^(?:inv|run|art|esc)-[a-f0-9]{10,32}$")
_GUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://")
_ARM_ID_RE = re.compile(r"(?i)(?:^|/)(?:subscriptions|resourcegroups|providers)/")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+|api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|sig=|"
    r"sharedaccesssignature|accountkey=)"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:raw|prompt|script|exception|traceback|incident_url|resource_id|subscription_id|"
    r"provider_token|access_token|secret|password|credential|connection_string)"
)

_PUBLIC_PAYLOAD_STRING_KEYS = {
    "status",
    "phase",
    "trigger",
    "requested_backend",
    "actual_backend",
    "backend",
    "reason_code",
    "decision",
    "confidence",
    "agent_key",
    "source",
    "category",
    "severity",
    "tool_name",
}
_PUBLIC_PAYLOAD_NUMBER_KEYS = {
    "count",
    "duration_ms",
    "model_calls",
    "total_tokens",
    "estimated_cost_usd",
    "finding_count",
    "source_count",
    "specialist_count",
    "result_count",
}
_PUBLIC_PAYLOAD_BOOLEAN_KEYS = {"schema_valid", "debate", "approval_required"}
_PUBLIC_PAYLOAD_OPAQUE_ID_KEYS = {"thread_id"}
_PUBLIC_PAYLOAD_LIST_KEYS = {
    "specialist_agents",
    "source_types",
    "finding_ids",
    "valid_evidence_ids",
    "missing_evidence",
    "tool_names",
}
_UNSET = object()

_INVESTIGATION_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS investigations (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        origin TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN (
            'opened', 'in_progress', 'analysis_ready', 'needs_review',
            'approved', 'rejected', 'closed_unverified', 'failed'
        )),
        scenario_id TEXT,
        escalation_id TEXT
    )
    """

_RUN_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        investigation_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'running', 'completed', 'insufficient_evidence', 'invalid_output', 'failed'
        )),
        requested_backend TEXT NOT NULL,
        actual_backend TEXT,
        usage_json TEXT,
        FOREIGN KEY (investigation_id) REFERENCES investigations(id)
    )
    """

_EVENT_KIND_CHECK_VALUES = EVENT_KINDS + _LEGACY_EVENT_KINDS
_EVENT_KIND_CHECK_SQL = ", ".join(f"'{value}'" for value in _EVENT_KIND_CHECK_VALUES)
_ARTIFACT_KIND_CHECK_VALUES = ARTIFACT_KINDS + _LEGACY_ARTIFACT_KINDS
_ARTIFACT_KIND_CHECK_SQL = ", ".join(f"'{value}'" for value in _ARTIFACT_KIND_CHECK_VALUES)

_EVENT_TABLE_SQL = f"""
    CREATE TABLE IF NOT EXISTS activity_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        investigation_id TEXT NOT NULL,
        run_id TEXT,
        occurred_at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN (
            'system', 'operator', 'detector', 'sre_agent',
            'orchestrator', 'specialist', 'reviewer'
        )),
        kind TEXT NOT NULL CHECK (kind IN ({_EVENT_KIND_CHECK_SQL})),
        provenance TEXT NOT NULL CHECK (provenance IN (
            'observed', 'reported', 'configured', 'simulated',
            'approved', 'executed', 'verified'
        )),
        payload_json TEXT NOT NULL,
        FOREIGN KEY (investigation_id) REFERENCES investigations(id),
        FOREIGN KEY (run_id) REFERENCES runs(id)
    )
    """

_ARTIFACT_TABLE_SQL = f"""
    CREATE TABLE IF NOT EXISTS activity_artifacts (
        id TEXT PRIMARY KEY,
        investigation_id TEXT NOT NULL,
        run_id TEXT,
        created_at TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ({_ARTIFACT_KIND_CHECK_SQL})),
        provenance TEXT NOT NULL CHECK (provenance IN (
            'observed', 'reported', 'configured', 'simulated',
            'approved', 'executed', 'verified'
        )),
        payload_json TEXT NOT NULL,
        FOREIGN KEY (investigation_id) REFERENCES investigations(id),
        FOREIGN KEY (run_id) REFERENCES runs(id)
    )
    """

_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS ix_investigations_created ON investigations(created_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_investigations_escalation ON investigations(escalation_id)",
    "CREATE INDEX IF NOT EXISTS ix_runs_investigation ON runs(investigation_id, started_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_activity_events_investigation ON activity_events(investigation_id, seq)",
    "CREATE INDEX IF NOT EXISTS ix_activity_events_run ON activity_events(run_id, seq)",
    "CREATE INDEX IF NOT EXISTS ix_activity_artifacts_investigation ON activity_artifacts(investigation_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_activity_artifacts_run ON activity_artifacts(run_id, created_at, id)",
)


class ActivityValidationError(ValueError):
    """A producer supplied a value outside the locked Activity contract."""


class ActivityStoreError(RuntimeError):
    """The durable Activity store could not complete an operation."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_timestamp(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ActivityValidationError(f"{field_name} must be a bounded ISO-8601 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ActivityValidationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ActivityValidationError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _validate_token(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ActivityValidationError(
            f"{field_name} must match {_TOKEN_RE.pattern!r}"
        )
    return value


def _validate_optional_link(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ActivityValidationError(f"{field_name} must be null or a non-empty string up to 64 characters")
    if field_name == "escalation_id" and not _OPAQUE_ID_RE.fullmatch(value):
        raise ActivityValidationError("escalation_id must be an opaque escalation id")
    if field_name == "scenario_id" and not _SCENARIO_ID_RE.fullmatch(value):
        raise ActivityValidationError("scenario_id must be a safe scenario token")
    return value


def _validate_json_value(value, *, field_name: str, max_bytes: int) -> str:
    def walk(item, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise ActivityValidationError(f"{field_name} exceeds maximum JSON depth {MAX_JSON_DEPTH}")
        if item is None or isinstance(item, (str, bool, int)):
            if isinstance(item, str) and len(item) > MAX_STRING_LENGTH:
                raise ActivityValidationError(
                    f"{field_name} string values must be at most {MAX_STRING_LENGTH} characters"
                )
            if isinstance(item, int) and not isinstance(item, bool) and abs(item) > MAX_NUMBER_ABS:
                raise ActivityValidationError(
                    f"{field_name} integer values must be between {-MAX_NUMBER_ABS} and {MAX_NUMBER_ABS}"
                )
            return
        if isinstance(item, float):
            if not math.isfinite(item) or abs(item) > MAX_NUMBER_ABS:
                raise ActivityValidationError(
                    f"{field_name} numbers must be finite and between {-MAX_NUMBER_ABS} and {MAX_NUMBER_ABS}"
                )
            return
        if isinstance(item, list):
            if len(item) > MAX_CONTAINER_ITEMS:
                raise ActivityValidationError(
                    f"{field_name} arrays must contain at most {MAX_CONTAINER_ITEMS} items"
                )
            for child in item:
                walk(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > MAX_CONTAINER_ITEMS:
                raise ActivityValidationError(
                    f"{field_name} objects must contain at most {MAX_CONTAINER_ITEMS} keys"
                )
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 64:
                    raise ActivityValidationError(
                        f"{field_name} object keys must be non-empty strings up to 64 characters"
                    )
                walk(child, depth + 1)
            return
        raise ActivityValidationError(f"{field_name} must contain only JSON scalar, array, or object values")

    walk(value, 0)
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ActivityValidationError(f"{field_name} exceeds the {max_bytes}-byte limit")
    return encoded


def _decode_json(value: Optional[str], default):
    if not value:
        return default
    decoded = json.loads(value)
    return decoded


def _safe_public_string(value: str) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > MAX_PUBLIC_STRING_LENGTH:
        return None
    if _GUID_RE.search(value) or _URL_RE.search(value) or _ARM_ID_RE.search(value) or _SECRET_VALUE_RE.search(value):
        return None
    return value


def _safe_public_token(value: str) -> Optional[str]:
    safe = _safe_public_string(value)
    if safe is None or not _TOKEN_RE.fullmatch(safe):
        return None
    return safe


def _safe_public_id(value: str) -> Optional[str]:
    safe = _safe_public_string(value)
    if safe is None or not _SAFE_PUBLIC_ID_RE.fullmatch(safe):
        return None
    return safe


def _safe_public_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if abs(value) <= MAX_NUMBER_ABS else None
    return value if math.isfinite(value) and abs(value) <= MAX_NUMBER_ABS else None


def _safe_public_timestamp(value: str) -> Optional[str]:
    try:
        return _normalize_timestamp(value, "public timestamp")
    except ActivityValidationError:
        return None


def _public_citation(value) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    projected = {}
    finding_id = _safe_public_id(value.get("finding_id"))
    if finding_id is not None:
        projected["finding_id"] = finding_id
    for key in ("source", "category", "severity"):
        safe = _safe_public_token(value.get(key))
        if safe is not None:
            projected[key] = safe
    observed_at = _safe_public_timestamp(value.get("observed_at"))
    if observed_at is not None:
        projected["observed_at"] = observed_at
    references = []
    for reference in value.get("references") or ():
        if not isinstance(reference, dict):
            continue
        public_reference = {}
        source = _safe_public_token(reference.get("source"))
        if source is not None:
            public_reference["source"] = source
        observed_at = _safe_public_timestamp(reference.get("observed_at"))
        if observed_at is not None:
            public_reference["observed_at"] = observed_at
        if public_reference:
            references.append(public_reference)
        if len(references) >= 3:
            break
    if references:
        projected["references"] = references
    return projected or None


def _public_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    projected = {}
    for key, value in payload.items():
        if _SENSITIVE_KEY_RE.search(key):
            continue
        if key in _PUBLIC_PAYLOAD_STRING_KEYS:
            safe = _safe_public_token(value)
            if safe is not None:
                projected[key] = safe
        elif key in _PUBLIC_PAYLOAD_OPAQUE_ID_KEYS:
            safe = _safe_public_id(value)
            if safe is not None:
                projected[key] = safe
        elif key in _PUBLIC_PAYLOAD_NUMBER_KEYS:
            safe = _safe_public_number(value)
            if safe is not None:
                projected[key] = safe
        elif key == "round":
            safe = _safe_public_number(value)
            if safe is None:
                safe = _safe_public_token(value)
            if safe is not None:
                projected[key] = safe
        elif key in _PUBLIC_PAYLOAD_BOOLEAN_KEYS:
            if isinstance(value, bool):
                projected[key] = value
        elif key in _PUBLIC_PAYLOAD_LIST_KEYS and isinstance(value, list):
            safe_items = []
            for item in value[:20]:
                safe = _safe_public_id(item)
                if safe is not None:
                    safe_items.append(safe)
            projected[key] = safe_items
        elif key == "citations" and isinstance(value, list):
            citations = []
            for item in value[:25]:
                citation = _public_citation(item)
                if citation is not None:
                    citations.append(citation)
            projected[key] = citations
    return projected


def _encode_cursor(created_at: str, investigation_id: str) -> str:
    raw = json.dumps([created_at, investigation_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise ActivityValidationError("cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivityValidationError("cursor is invalid") from exc
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not isinstance(value[1], str)
        or not value[0]
        or not value[1]
    ):
        raise ActivityValidationError("cursor is invalid")
    return value[0], value[1]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _table_sql(conn: sqlite3.Connection, table_name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return (row[0] or "") if row is not None else ""


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _migrate_runs_table(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "runs"):
        conn.execute(_RUN_TABLE_SQL)
        return False
    columns = _table_columns(conn, "runs")
    if {"started_at", "completed_at"}.issubset(columns):
        return False
    started_at = "started_at" if "started_at" in columns else "created_at"
    if "completed_at" in columns:
        completed_at = "completed_at"
    elif "updated_at" in columns:
        completed_at = "CASE WHEN status = 'running' THEN NULL ELSE updated_at END"
    else:
        completed_at = "NULL"
    conn.execute("ALTER TABLE runs RENAME TO activity_runs_legacy")
    conn.execute(_RUN_TABLE_SQL)
    conn.execute(
        "INSERT INTO runs "
        "(id, investigation_id, started_at, completed_at, trigger, status, "
        "requested_backend, actual_backend, usage_json) "
        f"SELECT id, investigation_id, {started_at}, {completed_at}, trigger, status, "
        "requested_backend, actual_backend, usage_json FROM activity_runs_legacy"
    )
    conn.execute("DROP TABLE activity_runs_legacy")
    return True


def _migrate_events_table(conn: sqlite3.Connection, *, force_rebuild: bool = False) -> None:
    if not _table_exists(conn, "activity_events"):
        conn.execute(_EVENT_TABLE_SQL)
        return
    sql = _table_sql(conn, "activity_events")
    if not force_rebuild and all(f"'{kind}'" in sql for kind in EVENT_KINDS):
        return
    conn.execute("ALTER TABLE activity_events RENAME TO activity_events_legacy")
    conn.execute(_EVENT_TABLE_SQL)
    conn.execute(
        "INSERT INTO activity_events "
        "(seq, investigation_id, run_id, occurred_at, actor, kind, provenance, payload_json) "
        "SELECT seq, investigation_id, run_id, occurred_at, actor, kind, provenance, payload_json "
        "FROM activity_events_legacy ORDER BY seq"
    )
    conn.execute("DROP TABLE activity_events_legacy")


def _migrate_artifacts_table(conn: sqlite3.Connection, *, force_rebuild: bool = False) -> None:
    if not _table_exists(conn, "activity_artifacts"):
        conn.execute(_ARTIFACT_TABLE_SQL)
        return
    sql = _table_sql(conn, "activity_artifacts")
    if not force_rebuild and all(f"'{kind}'" in sql for kind in ARTIFACT_KINDS):
        return
    conn.execute("ALTER TABLE activity_artifacts RENAME TO activity_artifacts_legacy")
    conn.execute(_ARTIFACT_TABLE_SQL)
    conn.execute(
        "INSERT INTO activity_artifacts "
        "(id, investigation_id, run_id, created_at, kind, provenance, payload_json) "
        "SELECT id, investigation_id, run_id, created_at, kind, provenance, payload_json "
        "FROM activity_artifacts_legacy"
    )
    conn.execute("DROP TABLE activity_artifacts_legacy")


def ensure_activity_schema(db_path: str) -> None:
    """Apply the additive Activity migration and optional ledger link atomically."""
    if not db_path or not db_path.strip():
        raise ValueError("db_path must not be blank")
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_INVESTIGATION_TABLE_SQL)
        runs_rebuilt = _migrate_runs_table(conn)
        _migrate_events_table(conn, force_rebuild=runs_rebuilt)
        _migrate_artifacts_table(conn, force_rebuild=runs_rebuilt)
        ledger_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'zeroops_escalations'"
        ).fetchone()
        if ledger_exists:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(zeroops_escalations)").fetchall()}
            if "investigation_id" not in columns:
                conn.execute("ALTER TABLE zeroops_escalations ADD COLUMN investigation_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_zeroops_escalations_investigation "
                "ON zeroops_escalations(investigation_id)"
            )
        for statement in _INDEX_STATEMENTS:
            conn.execute(statement)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError("activity migration would leave foreign key violations")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()


class ActivityStore:
    """Durable writer APIs plus allowlisted public Activity projections."""

    def __init__(self, db_path: str):
        if not db_path or not db_path.strip():
            raise ValueError("db_path must not be blank")
        self.db_path = db_path
        self._lock = threading.RLock()
        try:
            ensure_activity_schema(db_path)
        except (OSError, sqlite3.Error) as exc:
            raise ActivityStoreError("activity store migration failed") from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _write(self, callback):
        with self._lock:
            try:
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = callback(conn)
                    conn.execute("COMMIT")
                    return result
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                finally:
                    conn.close()
            except (OSError, sqlite3.Error) as exc:
                raise ActivityStoreError("activity store write failed") from exc

    def _read(self, callback):
        try:
            conn = self._connect()
            try:
                return callback(conn)
            finally:
                conn.close()
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise ActivityStoreError("activity store read failed") from exc

    @staticmethod
    def _require_investigation(conn: sqlite3.Connection, investigation_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
        if row is None:
            raise KeyError(investigation_id)
        return row

    @classmethod
    def _require_run(
        cls, conn: sqlite3.Connection, investigation_id: str, run_id: Optional[str]
    ) -> Optional[sqlite3.Row]:
        if run_id is None:
            return None
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None or row["investigation_id"] != investigation_id:
            raise ActivityValidationError("run_id does not belong to investigation_id")
        return row

    def open_investigation(
        self,
        *,
        origin: str,
        phase: str = "opened",
        scenario_id: Optional[str] = None,
        escalation_id: Optional[str] = None,
    ) -> dict:
        if origin not in ORIGINS:
            raise ActivityValidationError(f"origin must be one of {ORIGINS}")
        if phase not in INVESTIGATION_PHASES:
            raise ActivityValidationError(f"phase must be one of {INVESTIGATION_PHASES}")
        scenario_id = _validate_optional_link(scenario_id, "scenario_id")
        escalation_id = _validate_optional_link(escalation_id, "escalation_id")
        investigation_id = _new_id("inv")
        now = _now()

        def write(conn):
            conn.execute(
                "INSERT INTO investigations "
                "(id, created_at, updated_at, origin, phase, scenario_id, escalation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (investigation_id, now, now, origin, phase, scenario_id, escalation_id),
            )
            return self._investigation_public(
                conn.execute("SELECT * FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
            )

        return self._write(write)

    def update_investigation(
        self,
        investigation_id: str,
        *,
        phase=_UNSET,
        scenario_id=_UNSET,
        escalation_id=_UNSET,
    ) -> dict:
        if phase is _UNSET and scenario_id is _UNSET and escalation_id is _UNSET:
            raise ActivityValidationError("at least one investigation field is required")
        if phase is not _UNSET and phase not in INVESTIGATION_PHASES:
            raise ActivityValidationError(f"phase must be one of {INVESTIGATION_PHASES}")
        if scenario_id is not _UNSET:
            scenario_id = _validate_optional_link(scenario_id, "scenario_id")
        if escalation_id is not _UNSET:
            escalation_id = _validate_optional_link(escalation_id, "escalation_id")

        def write(conn):
            current = self._require_investigation(conn, investigation_id)
            conn.execute(
                "UPDATE investigations SET updated_at = ?, phase = ?, scenario_id = ?, escalation_id = ? "
                "WHERE id = ?",
                (
                    _now(),
                    phase if phase is not _UNSET else current["phase"],
                    scenario_id if scenario_id is not _UNSET else current["scenario_id"],
                    escalation_id if escalation_id is not _UNSET else current["escalation_id"],
                    investigation_id,
                ),
            )
            return self._investigation_public(
                conn.execute("SELECT * FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
            )

        return self._write(write)

    def start_run(self, investigation_id: str, *, trigger: str, requested_backend: str) -> dict:
        if trigger not in TRIGGERS:
            raise ActivityValidationError(f"trigger must be one of {TRIGGERS}")
        requested_backend = _validate_token(requested_backend, "requested_backend")
        run_id = _new_id("run")
        now = _now()

        def write(conn):
            self._require_investigation(conn, investigation_id)
            conn.execute(
                "INSERT INTO runs "
                "(id, investigation_id, started_at, completed_at, trigger, status, requested_backend) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?)",
                (run_id, investigation_id, now, trigger, "running", requested_backend),
            )
            conn.execute("UPDATE investigations SET updated_at = ? WHERE id = ?", (now, investigation_id))
            return self._run_public(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

        return self._write(write)

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        actual_backend: Optional[str] = None,
        usage: Optional[dict] = None,
    ) -> dict:
        if status not in RUN_STATUSES or status == "running":
            raise ActivityValidationError(
                f"finished run status must be one of {RUN_STATUSES[1:]}"
            )
        if actual_backend is not None:
            actual_backend = _validate_token(actual_backend, "actual_backend")
        usage_json = None
        if usage is not None:
            if not isinstance(usage, dict):
                raise ActivityValidationError("usage must be a JSON object or null")
            usage_json = _validate_json_value(usage, field_name="usage", max_bytes=MAX_USAGE_BYTES)

        def write(conn):
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] != "running":
                raise ActivityValidationError("only a running run can be finished")
            now = _now()
            conn.execute(
                "UPDATE runs SET completed_at = ?, status = ?, actual_backend = ?, usage_json = ? WHERE id = ?",
                (now, status, actual_backend, usage_json, run_id),
            )
            conn.execute(
                "UPDATE investigations SET updated_at = ? WHERE id = ?",
                (now, row["investigation_id"]),
            )
            return self._run_public(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

        return self._write(write)

    def append_event(
        self,
        investigation_id: str,
        *,
        actor: str,
        kind: str,
        provenance: str,
        payload: dict,
        run_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
        idempotent: bool = False,
    ) -> dict:
        return self._append_event(
            investigation_id,
            actor=actor,
            kind=kind,
            provenance=provenance,
            payload=payload,
            run_id=run_id,
            occurred_at=occurred_at,
            idempotent=idempotent,
        )

    def has_event(
        self,
        investigation_id: str,
        *,
        actor: str,
        kind: str,
        provenance: str,
        payload: dict,
        run_id: Optional[str] = None,
    ) -> bool:
        if actor not in ACTORS:
            raise ActivityValidationError(f"actor must be one of {ACTORS}")
        if kind not in EVENT_KINDS:
            raise ActivityValidationError(f"kind must be one of {EVENT_KINDS}")
        if provenance not in WRITABLE_PROVENANCE:
            raise ActivityValidationError(f"provenance must be one of {WRITABLE_PROVENANCE}")
        if not isinstance(payload, dict):
            raise ActivityValidationError("payload must be a JSON object")
        _validate_json_value(payload, field_name="payload", max_bytes=MAX_PAYLOAD_BYTES)

        def read(conn):
            self._require_investigation(conn, investigation_id)
            self._require_run(conn, investigation_id, run_id)
            rows = conn.execute(
                "SELECT payload_json FROM activity_events WHERE investigation_id = ? AND run_id IS ? "
                "AND actor = ? AND kind = ? AND provenance = ? ORDER BY seq",
                (investigation_id, run_id, actor, kind, provenance),
            ).fetchall()
            return any(_decode_json(row["payload_json"], {}) == payload for row in rows)

        return self._read(read)

    def _append_event(
        self,
        investigation_id: str,
        *,
        actor: str,
        kind: str,
        provenance: str,
        payload: dict,
        run_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
        idempotent: bool,
    ) -> dict:
        if actor not in ACTORS:
            raise ActivityValidationError(f"actor must be one of {ACTORS}")
        if kind not in EVENT_KINDS:
            raise ActivityValidationError(f"kind must be one of {EVENT_KINDS}")
        if provenance not in WRITABLE_PROVENANCE:
            raise ActivityValidationError(f"provenance must be one of {WRITABLE_PROVENANCE}")
        if not isinstance(payload, dict):
            raise ActivityValidationError("payload must be a JSON object")
        payload_json = _validate_json_value(payload, field_name="payload", max_bytes=MAX_PAYLOAD_BYTES)
        occurred_at = _normalize_timestamp(occurred_at, "occurred_at") if occurred_at is not None else _now()

        def write(conn):
            self._require_investigation(conn, investigation_id)
            self._require_run(conn, investigation_id, run_id)
            if idempotent:
                rows = conn.execute(
                    "SELECT * FROM activity_events WHERE investigation_id = ? AND run_id IS ? "
                    "AND actor = ? AND kind = ? AND provenance = ? ORDER BY seq",
                    (investigation_id, run_id, actor, kind, provenance),
                ).fetchall()
                for row in rows:
                    if _decode_json(row["payload_json"], {}) == payload:
                        return self._event_public(row)
            cursor = conn.execute(
                "INSERT INTO activity_events "
                "(investigation_id, run_id, occurred_at, actor, kind, provenance, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (investigation_id, run_id, occurred_at, actor, kind, provenance, payload_json),
            )
            conn.execute(
                "UPDATE investigations SET updated_at = ? WHERE id = ?",
                (_now(), investigation_id),
            )
            row = conn.execute("SELECT * FROM activity_events WHERE seq = ?", (cursor.lastrowid,)).fetchone()
            return self._event_public(row)

        return self._write(write)

    def get_latest_run(self, investigation_id: str) -> Optional[dict]:
        def read(conn):
            self._require_investigation(conn, investigation_id)
            row = conn.execute(
                "SELECT * FROM runs WHERE investigation_id = ? ORDER BY started_at DESC, id DESC LIMIT 1",
                (investigation_id,),
            ).fetchone()
            return self._run_public(row) if row is not None else None

        return self._read(read)

    def append_artifact(
        self,
        investigation_id: str,
        *,
        kind: str,
        provenance: str,
        payload: dict,
        run_id: Optional[str] = None,
    ) -> dict:
        if kind not in ARTIFACT_KINDS:
            raise ActivityValidationError(f"kind must be one of {ARTIFACT_KINDS}")
        if provenance not in WRITABLE_PROVENANCE:
            raise ActivityValidationError(f"provenance must be one of {WRITABLE_PROVENANCE}")
        if not isinstance(payload, dict):
            raise ActivityValidationError("payload must be a JSON object")
        payload_json = _validate_json_value(payload, field_name="payload", max_bytes=MAX_PAYLOAD_BYTES)
        artifact_id = _new_id("art")
        now = _now()

        def write(conn):
            self._require_investigation(conn, investigation_id)
            self._require_run(conn, investigation_id, run_id)
            conn.execute(
                "INSERT INTO activity_artifacts "
                "(id, investigation_id, run_id, created_at, kind, provenance, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, investigation_id, run_id, now, kind, provenance, payload_json),
            )
            conn.execute("UPDATE investigations SET updated_at = ? WHERE id = ?", (now, investigation_id))
            row = conn.execute("SELECT * FROM activity_artifacts WHERE id = ?", (artifact_id,)).fetchone()
            return self._artifact_public(row)

        return self._write(write)

    @staticmethod
    def _investigation_public(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "origin": _safe_public_token(row["origin"]),
            "phase": row["phase"],
            "scenario_id": (
                row["scenario_id"]
                if row["scenario_id"] and _SCENARIO_ID_RE.fullmatch(row["scenario_id"])
                else None
            ),
            "escalation_id": (
                row["escalation_id"]
                if row["escalation_id"] and _OPAQUE_ID_RE.fullmatch(row["escalation_id"])
                else None
            ),
        }

    @staticmethod
    def _run_public(row: sqlite3.Row) -> dict:
        usage = _decode_json(row["usage_json"], None)
        public_usage = _public_payload(usage) if isinstance(usage, dict) else None
        return {
            "id": row["id"],
            "investigation_id": row["investigation_id"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "trigger": _safe_public_token(row["trigger"]),
            "status": row["status"],
            "requested_backend": _safe_public_token(row["requested_backend"]),
            "actual_backend": _safe_public_token(row["actual_backend"]) if row["actual_backend"] else None,
            "usage": public_usage,
        }

    @staticmethod
    def _event_public(row: sqlite3.Row) -> dict:
        return {
            "seq": row["seq"],
            "investigation_id": row["investigation_id"],
            "run_id": row["run_id"],
            "occurred_at": row["occurred_at"],
            "actor": row["actor"],
            "kind": row["kind"],
            "provenance": row["provenance"],
            "payload": _public_payload(_decode_json(row["payload_json"], {})),
        }

    @staticmethod
    def _artifact_public(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "investigation_id": row["investigation_id"],
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "kind": row["kind"],
            "provenance": row["provenance"],
            "payload": _public_payload(_decode_json(row["payload_json"], {})),
        }

    def list_public_investigations(self, *, limit: int = 20, cursor: Optional[str] = None) -> dict:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 50:
            raise ActivityValidationError("limit must be an integer from 1 to 50")
        cursor_values = _decode_cursor(cursor) if cursor is not None else None

        def read(conn):
            where = ""
            params = []
            if cursor_values is not None:
                where = "WHERE (created_at < ? OR (created_at = ? AND id < ?))"
                params.extend((cursor_values[0], cursor_values[0], cursor_values[1]))
            rows = conn.execute(
                "SELECT * FROM investigations "
                f"{where} ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            items = []
            for row in page:
                item = self._investigation_public(row)
                latest_run = conn.execute(
                    "SELECT * FROM runs WHERE investigation_id = ? ORDER BY started_at DESC, id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                counts = conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM runs WHERE investigation_id = ?) AS run_count, "
                    "(SELECT COUNT(*) FROM activity_events WHERE investigation_id = ?) AS event_count, "
                    "(SELECT COUNT(*) FROM activity_artifacts WHERE investigation_id = ?) AS artifact_count",
                    (row["id"], row["id"], row["id"]),
                ).fetchone()
                item["latest_run"] = self._run_public(latest_run) if latest_run is not None else None
                item["run_count"] = counts["run_count"]
                item["event_count"] = counts["event_count"]
                item["artifact_count"] = counts["artifact_count"]
                items.append(item)
            next_cursor = (
                _encode_cursor(page[-1]["created_at"], page[-1]["id"])
                if has_more and page
                else None
            )
            return {"items": items, "next_cursor": next_cursor}

        return self._read(read)

    def get_public_investigation(
        self, investigation_id: str, *, limit: int = 50, event_after: int = 0
    ) -> Optional[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
            raise ActivityValidationError("limit must be an integer from 1 to 100")
        if not isinstance(event_after, int) or isinstance(event_after, bool) or event_after < 0:
            raise ActivityValidationError("event_after must be a non-negative integer")

        def read(conn):
            investigation = conn.execute(
                "SELECT * FROM investigations WHERE id = ?", (investigation_id,)
            ).fetchone()
            if investigation is None:
                return None
            runs = conn.execute(
                "SELECT * FROM runs WHERE investigation_id = ? ORDER BY started_at, id",
                (investigation_id,),
            ).fetchall()
            event_rows = conn.execute(
                "SELECT * FROM activity_events WHERE investigation_id = ? AND seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (investigation_id, event_after, limit + 1),
            ).fetchall()
            has_more = len(event_rows) > limit
            event_page = event_rows[:limit]
            artifacts = conn.execute(
                "SELECT * FROM activity_artifacts WHERE investigation_id = ? ORDER BY created_at, id",
                (investigation_id,),
            ).fetchall()
            return {
                "investigation": self._investigation_public(investigation),
                "runs": [self._run_public(row) for row in runs],
                "events": [self._event_public(row) for row in event_page],
                "artifacts": [self._artifact_public(row) for row in artifacts],
                "next_event_cursor": event_page[-1]["seq"] if has_more and event_page else None,
            }

        return self._read(read)


_STORE = None
_STORE_LOCK = threading.Lock()


def get_activity_store() -> ActivityStore:
    global _STORE
    path = os.environ.get("OPERATIONS_STATE_DB", "").strip() or "operations_state.db"
    with _STORE_LOCK:
        if _STORE is None or _STORE.db_path != path:
            _STORE = ActivityStore(path)
        return _STORE
