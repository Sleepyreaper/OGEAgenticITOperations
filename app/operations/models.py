"""Structured evidence model for the operations layer.

Everything in this module is a plain, JSON-serializable dataclass with
strict validation in `__post_init__` -- malformed input raises
`ValueError`/`TypeError` immediately rather than being silently coerced
or dropped. Timestamps are always normalized to UTC ISO-8601
(`...Z`, millisecond precision) via `ensure_utc_iso`. IDs are
deterministic (see app/operations/identifiers.py), never random, so the
same underlying Azure state re-collected later produces the same ID.

See docs/EVIDENCE_MODEL.md for the full schema, the deterministic-vs-AI
boundary, and worked examples.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.operations.identifiers import compute_action_item_id, compute_finding_id

__all__ = [
    "EvidenceSource",
    "FindingCategory",
    "Severity",
    "FindingStatus",
    "ConfidenceLevel",
    "EvidenceReference",
    "Finding",
    "ActionItem",
    "SLOSummary",
    "CapacitySummary",
    "BudgetSummary",
    "TelemetryCoverageSummary",
    "utc_now",
    "utc_now_iso",
    "format_utc_iso",
    "parse_utc_iso",
    "ensure_utc_iso",
]


# ─── Enums (the closed vocabularies every Finding/reference validates against) ──

class EvidenceSource(str, Enum):
    """Canonical, machine-checkable origin of an EvidenceReference/Finding.

    Extend this enum (never invent a free-text source string) when a new
    collector is added -- EvidenceReference.__post_init__ rejects any
    value not listed here.
    """
    AZURE_MONITOR_ALERT = "azure_monitor_alert"
    ACTIVITY_LOG = "activity_log"
    RESOURCE_HEALTH = "resource_health"
    SERVICE_HEALTH = "service_health"
    LOG_ANALYTICS_SLO = "log_analytics_slo"
    ARM_COMPUTE_USAGE = "arm_compute_usage"
    ARM_OPENAI_QUOTA = "arm_openai_quota"
    ADVISOR = "advisor"
    POLICY_INSIGHTS = "policy_insights"
    RESOURCE_GRAPH = "resource_graph"
    MANUAL = "manual"
    # ── Phase 2: operational risk/hygiene collectors ──
    DEFENDER_ALERT = "microsoft_defender_alert"
    DEFENDER_ASSESSMENT = "microsoft_defender_assessment"
    COST_MANAGEMENT_BUDGET = "cost_management_budget"
    COST_MANAGEMENT_USAGE = "cost_management_usage"
    BACKUP_JOB = "azure_backup_job"
    BACKUP_PROTECTED_ITEM = "azure_backup_protected_item"
    UPDATE_MANAGER = "azure_update_manager"
    KEY_VAULT_EXPIRY = "key_vault_expiry"
    AUTOMATION_JOB = "azure_automation_job"
    TELEMETRY_COVERAGE = "telemetry_coverage"


class FindingCategory(str, Enum):
    INCIDENT = "incident"
    RELIABILITY = "reliability"
    CAPACITY = "capacity"
    CHANGE = "change"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    COST = "cost"
    BACKUP = "backup"
    PATCH = "patch"
    CERTIFICATE = "certificate"
    AUTOMATION = "automation"
    TELEMETRY = "telemetry"
    OWNERSHIP = "ownership"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"


# Lower rank = more urgent. Used by app.operations.priority, not stored as
# a score anywhere -- exposed for transparency, not as an opaque number.
SEVERITY_RANK = {
    Severity.CRITICAL.value: 0,
    Severity.HIGH.value: 1,
    Severity.MEDIUM.value: 2,
    Severity.LOW.value: 3,
    Severity.INFORMATIONAL.value: 4,
}


class FindingStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


class ConfidenceLevel(str, Enum):
    """How a Finding's facts were established -- the deterministic-vs-AI
    boundary lives entirely inside this enum; nothing in this package
    ever assigns a confidence level based on model output.

      confirmed  -- a platform directly reported this fact (an Azure
                    Monitor alert, a Resource Health status, an Advisor
                    recommendation, an Activity Log 'Failed' outcome).
      derived    -- deterministically computed from platform data via
                    explicit, documented thresholds/math (a capacity
                    threshold state, an SLO error-budget state).
      correlated -- deterministically correlated across two or more
                    platform signals via explicit timestamp/resource
                    matching (a change matched to a health event).
      estimated  -- a deterministic forecast/extrapolation from historical
                    data points (a capacity exhaustion date).
    """
    CONFIRMED = "confirmed"
    DERIVED = "derived"
    CORRELATED = "correlated"
    ESTIMATED = "estimated"


CONFIDENCE_RANK = {
    ConfidenceLevel.CONFIRMED.value: 0,
    ConfidenceLevel.CORRELATED.value: 1,
    ConfidenceLevel.DERIVED.value: 2,
    ConfidenceLevel.ESTIMATED.value: 3,
}


# ─── Timestamp helpers -- every Finding/EvidenceReference timestamp goes
# through these so storage/comparison/ID-hashing all see one canonical
# string form regardless of how a given Azure API formatted its input. ──

_MAX_EXCERPT_CHARS = 400
# Defense-in-depth only -- callers must never pass raw credentials into
# raw_excerpt in the first place. This redacts the common
# "key: value"/"key=value" shapes a copy-pasted header or connection
# string would take, plus bare Bearer tokens.
_CREDENTIAL_KV_PATTERN = re.compile(
    r"(?i)\b(authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|"
    r"password|secret|sas[-_]?token|connection[-_]?string)\b\s*[:=]\s*\S+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{10,}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc_iso(value: datetime) -> str:
    """Canonical UTC ISO-8601 string, millisecond precision, 'Z' suffix."""
    if value.tzinfo is None:
        raise ValueError("format_utc_iso requires a timezone-aware datetime (got a naive one)")
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def utc_now_iso() -> str:
    return format_utc_iso(utc_now())


def parse_utc_iso(value: str, *, field_name: str = "timestamp") -> datetime:
    """Parse an ISO-8601 timestamp string, requiring it to be tz-aware.

    A naive (no offset/'Z') input is rejected outright -- this module
    never guesses which timezone an unmarked timestamp is in.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}: expected a non-empty ISO-8601 timestamp string, got {value!r}")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name}: invalid ISO-8601 timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{field_name}: timestamp {value!r} is not timezone-aware; UTC (e.g. a 'Z' suffix) is required"
        )
    return parsed.astimezone(timezone.utc)


def ensure_utc_iso(value, *, field_name: str = "timestamp") -> str:
    """Accept a tz-aware datetime or an ISO-8601 string; return the
    canonical UTC ISO-8601 string form."""
    if isinstance(value, datetime):
        return format_utc_iso(value)
    return format_utc_iso(parse_utc_iso(value, field_name=field_name))


def _sanitize_excerpt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    # Bearer-token redaction must run BEFORE the generic key/value
    # redaction below: "Authorization: Bearer <token>" would otherwise
    # have only the literal word "Bearer" consumed by the key/value
    # pattern's single-token match, leaving the actual token dangling
    # and unredacted.
    text = _BEARER_PATTERN.sub(lambda m: "Bearer [REDACTED]", text)
    text = _CREDENTIAL_KV_PATTERN.sub(lambda m: f"{m.group(1)}=***REDACTED***", text)
    if len(text) > _MAX_EXCERPT_CHARS:
        omitted = len(text) - _MAX_EXCERPT_CHARS
        text = f"{text[:_MAX_EXCERPT_CHARS]}... [truncated {omitted} chars]"
    return text


# ─── Evidence ────────────────────────────────────────────────────────

@dataclass
class EvidenceReference:
    """One citation backing a Finding -- never a full raw Azure payload
    dump, and never a credential (raw_excerpt is defensively sanitized;
    see _sanitize_excerpt)."""
    source: str
    title: str
    observed_at: str
    resource_id: Optional[str] = None
    reference: Optional[str] = None
    raw_excerpt: Optional[str] = None

    def __post_init__(self):
        valid_sources = {member.value for member in EvidenceSource}
        if self.source not in valid_sources:
            raise ValueError(f"EvidenceReference.source {self.source!r} is not a recognized EvidenceSource")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("EvidenceReference.title is required")
        self.observed_at = ensure_utc_iso(self.observed_at, field_name="EvidenceReference.observed_at")
        self.raw_excerpt = _sanitize_excerpt(self.raw_excerpt)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "observed_at": self.observed_at,
            "resource_id": self.resource_id,
            "reference": self.reference,
            "raw_excerpt": self.raw_excerpt,
        }


@dataclass
class Finding:
    """One deterministic, evidence-backed operational fact.

    `id` is computed deterministically (see identifiers.compute_finding_id)
    from (category, source, resource_id, discriminator) when not supplied
    explicitly -- pass `discriminator` whenever category/source/resource_id
    alone wouldn't be unique (e.g. an alert ID, an SLO workload name).
    """
    category: str
    severity: str
    status: str
    title: str
    summary: str
    business_impact: str
    first_seen: str
    last_seen: str
    source: str
    id: str = ""
    owner: str = ""
    resource_id: Optional[str] = None
    affected_resource_count: int = 0
    affected_workload_count: int = 0
    confidence: str = ConfidenceLevel.DERIVED.value
    evidence: list = field(default_factory=list)
    recommended_action: str = ""
    approval_required: bool = False
    executive_attention: bool = False
    metadata: dict = field(default_factory=dict)
    discriminator: str = ""

    def __post_init__(self):
        valid_categories = {member.value for member in FindingCategory}
        if self.category not in valid_categories:
            raise ValueError(f"Finding.category {self.category!r} is not a recognized FindingCategory")
        valid_severities = {member.value for member in Severity}
        if self.severity not in valid_severities:
            raise ValueError(f"Finding.severity {self.severity!r} is not a recognized Severity")
        valid_statuses = {member.value for member in FindingStatus}
        if self.status not in valid_statuses:
            raise ValueError(f"Finding.status {self.status!r} is not a recognized FindingStatus")
        valid_confidence = {member.value for member in ConfidenceLevel}
        if self.confidence not in valid_confidence:
            raise ValueError(f"Finding.confidence {self.confidence!r} is not a recognized ConfidenceLevel")
        if not self.title.strip():
            raise ValueError("Finding.title is required")
        if not self.summary.strip():
            raise ValueError("Finding.summary is required")
        if not self.source.strip():
            raise ValueError("Finding.source is required")

        self.first_seen = ensure_utc_iso(self.first_seen, field_name="Finding.first_seen")
        self.last_seen = ensure_utc_iso(self.last_seen, field_name="Finding.last_seen")
        if parse_utc_iso(self.last_seen) < parse_utc_iso(self.first_seen):
            raise ValueError(
                f"Finding.last_seen ({self.last_seen}) is before Finding.first_seen ({self.first_seen})"
            )

        if self.affected_resource_count < 0:
            raise ValueError("Finding.affected_resource_count must be >= 0")
        if self.affected_workload_count < 0:
            raise ValueError("Finding.affected_workload_count must be >= 0")

        for i, item in enumerate(self.evidence):
            if not isinstance(item, EvidenceReference):
                raise TypeError(f"Finding.evidence[{i}] must be an EvidenceReference instance, got {type(item)!r}")

        if not self.id:
            self.id = compute_finding_id(
                category=self.category,
                source=self.source,
                resource_id=self.resource_id,
                discriminator=self.discriminator or self.title,
            )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "severity": self.severity,
            "status": self.status,
            "title": self.title,
            "summary": self.summary,
            "business_impact": self.business_impact,
            "owner": self.owner,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "source": self.source,
            "resource_id": self.resource_id,
            "affected_resource_count": self.affected_resource_count,
            "affected_workload_count": self.affected_workload_count,
            "confidence": self.confidence,
            "evidence": [item.to_dict() for item in self.evidence],
            "recommended_action": self.recommended_action,
            "approval_required": self.approval_required,
            "executive_attention": self.executive_attention,
            "metadata": self.metadata,
        }


_VALID_ACTION_ITEM_STATUSES = {"proposed", "approved", "in_progress", "done", "rejected"}


@dataclass
class ActionItem:
    """A proposed next step tied back to a Finding. Intentionally separate
    from app.ado_integration.AdoProposal -- this is the evidence layer's
    own, ADO-agnostic representation; a future integration can translate
    one into the other."""
    finding_id: str
    title: str
    description: str = ""
    owner: str = ""
    approval_required: bool = False
    status: str = "proposed"
    due_by: Optional[str] = None
    id: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.finding_id.strip():
            raise ValueError("ActionItem.finding_id is required")
        if not self.title.strip():
            raise ValueError("ActionItem.title is required")
        if self.status not in _VALID_ACTION_ITEM_STATUSES:
            raise ValueError(f"ActionItem.status {self.status!r} must be one of {sorted(_VALID_ACTION_ITEM_STATUSES)}")
        if self.due_by is not None:
            self.due_by = ensure_utc_iso(self.due_by, field_name="ActionItem.due_by")
        if not self.id:
            self.id = compute_action_item_id(finding_id=self.finding_id, title=self.title)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "finding_id": self.finding_id,
            "title": self.title,
            "description": self.description,
            "owner": self.owner,
            "approval_required": self.approval_required,
            "status": self.status,
            "due_by": self.due_by,
            "metadata": self.metadata,
        }


_VALID_SLO_STATES = {"healthy", "at_risk", "breached", "insufficient_data"}
_VALID_CRITICALITY = {"customer_facing", "internal", "best_effort"}


@dataclass
class SLOSummary:
    """One workload's SLO evaluation for one window. `state` is always one
    of the values above -- there is no 'not_configured' state at this
    level (that's a collection-envelope-level concept for "no SLOs were
    defined at all"; see app/operations/service.py)."""
    workload: str
    state: str
    objective_pct: float
    observed_pct: Optional[float]
    window_hours: int
    criticality: str
    evaluated_at: str
    good_count: Optional[int] = None
    total_count: Optional[int] = None
    error_budget_remaining_pct: Optional[float] = None
    burn_rate: Optional[float] = None
    evidence: list = field(default_factory=list)

    def __post_init__(self):
        if not self.workload.strip():
            raise ValueError("SLOSummary.workload is required")
        if self.state not in _VALID_SLO_STATES:
            raise ValueError(f"SLOSummary.state {self.state!r} must be one of {sorted(_VALID_SLO_STATES)}")
        if not (0 < self.objective_pct <= 100):
            raise ValueError(f"SLOSummary.objective_pct must be in (0, 100], got {self.objective_pct}")
        if self.observed_pct is not None and not (0 <= self.observed_pct <= 100):
            raise ValueError(f"SLOSummary.observed_pct must be in [0, 100], got {self.observed_pct}")
        if self.window_hours <= 0:
            raise ValueError("SLOSummary.window_hours must be positive")
        if self.criticality not in _VALID_CRITICALITY:
            raise ValueError(f"SLOSummary.criticality {self.criticality!r} must be one of {sorted(_VALID_CRITICALITY)}")
        self.evaluated_at = ensure_utc_iso(self.evaluated_at, field_name="SLOSummary.evaluated_at")
        for i, item in enumerate(self.evidence):
            if not isinstance(item, EvidenceReference):
                raise TypeError(f"SLOSummary.evidence[{i}] must be an EvidenceReference instance")

    def to_dict(self) -> dict:
        return {
            "workload": self.workload,
            "state": self.state,
            "objective_pct": self.objective_pct,
            "observed_pct": self.observed_pct,
            "window_hours": self.window_hours,
            "criticality": self.criticality,
            "evaluated_at": self.evaluated_at,
            "good_count": self.good_count,
            "total_count": self.total_count,
            "error_budget_remaining_pct": self.error_budget_remaining_pct,
            "burn_rate": self.burn_rate,
            "evidence": [item.to_dict() for item in self.evidence],
        }


_VALID_THRESHOLD_STATES = {"healthy", "warning", "critical", "unknown"}
_VALID_FORECAST_STATES = {"available", "not_applicable", "not_available"}


@dataclass
class CapacitySummary:
    """One quota/usage line item (e.g. one Compute vCPU family in one
    region, or one Cognitive Services quota name in one region)."""
    resource_scope: str
    metric: str
    current: float
    limit: float
    threshold_state: str
    evaluated_at: str
    headroom_pct: Optional[float] = None
    forecast_state: str = "not_available"
    forecast_exhaustion_at: Optional[str] = None
    evidence: list = field(default_factory=list)

    def __post_init__(self):
        if not self.resource_scope.strip():
            raise ValueError("CapacitySummary.resource_scope is required")
        if not self.metric.strip():
            raise ValueError("CapacitySummary.metric is required")
        if self.current < 0:
            raise ValueError("CapacitySummary.current must be >= 0")
        if self.limit < 0:
            raise ValueError("CapacitySummary.limit must be >= 0")
        if self.threshold_state not in _VALID_THRESHOLD_STATES:
            raise ValueError(
                f"CapacitySummary.threshold_state {self.threshold_state!r} must be one of {sorted(_VALID_THRESHOLD_STATES)}"
            )
        if self.forecast_state not in _VALID_FORECAST_STATES:
            raise ValueError(
                f"CapacitySummary.forecast_state {self.forecast_state!r} must be one of {sorted(_VALID_FORECAST_STATES)}"
            )
        if self.forecast_state != "available" and self.forecast_exhaustion_at is not None:
            raise ValueError("CapacitySummary.forecast_exhaustion_at may only be set when forecast_state == 'available'")
        self.evaluated_at = ensure_utc_iso(self.evaluated_at, field_name="CapacitySummary.evaluated_at")
        if self.forecast_exhaustion_at is not None:
            self.forecast_exhaustion_at = ensure_utc_iso(
                self.forecast_exhaustion_at, field_name="CapacitySummary.forecast_exhaustion_at"
            )
        for i, item in enumerate(self.evidence):
            if not isinstance(item, EvidenceReference):
                raise TypeError(f"CapacitySummary.evidence[{i}] must be an EvidenceReference instance")

    def to_dict(self) -> dict:
        return {
            "resource_scope": self.resource_scope,
            "metric": self.metric,
            "current": self.current,
            "limit": self.limit,
            "headroom_pct": self.headroom_pct,
            "threshold_state": self.threshold_state,
            "forecast_state": self.forecast_state,
            "forecast_exhaustion_at": self.forecast_exhaustion_at,
            "evaluated_at": self.evaluated_at,
            "evidence": [item.to_dict() for item in self.evidence],
        }


_VALID_BUDGET_STATES = {"healthy", "warning", "critical", "unknown"}


@dataclass
class BudgetSummary:
    """One Azure Cost Management budget's threshold state (Microsoft.
    Consumption/budgets). Every configured budget gets a summary --
    matching CapacitySummary/SLOSummary's "surface the healthy ones too"
    convention -- while only warning/critical budgets become Findings
    (see collectors.cost.budget_summaries_to_findings)."""
    budget_name: str
    amount: float
    current_spend: float
    currency: str
    time_grain: str
    threshold_state: str
    evaluated_at: str
    usage_pct: Optional[float] = None
    category: str = "Cost"
    evidence: list = field(default_factory=list)

    def __post_init__(self):
        if not self.budget_name.strip():
            raise ValueError("BudgetSummary.budget_name is required")
        if self.amount < 0:
            raise ValueError("BudgetSummary.amount must be >= 0")
        if self.current_spend < 0:
            raise ValueError("BudgetSummary.current_spend must be >= 0")
        if self.threshold_state not in _VALID_BUDGET_STATES:
            raise ValueError(
                f"BudgetSummary.threshold_state {self.threshold_state!r} must be one of {sorted(_VALID_BUDGET_STATES)}"
            )
        self.evaluated_at = ensure_utc_iso(self.evaluated_at, field_name="BudgetSummary.evaluated_at")
        for i, item in enumerate(self.evidence):
            if not isinstance(item, EvidenceReference):
                raise TypeError(f"BudgetSummary.evidence[{i}] must be an EvidenceReference instance")

    def to_dict(self) -> dict:
        return {
            "budget_name": self.budget_name,
            "amount": self.amount,
            "current_spend": self.current_spend,
            "currency": self.currency,
            "time_grain": self.time_grain,
            "category": self.category,
            "usage_pct": self.usage_pct,
            "threshold_state": self.threshold_state,
            "evaluated_at": self.evaluated_at,
            "evidence": [item.to_dict() for item in self.evidence],
        }


_VALID_TELEMETRY_GAP_TYPES = {"diagnostic_settings", "heartbeat"}


@dataclass
class TelemetryCoverageSummary:
    """The explicit coverage denominator for one telemetry-gap check
    (diagnostic settings or Log Analytics heartbeat) -- so a caller can
    say "22 of 30 monitored resources have diagnostic settings" instead
    of only ever seeing the gap Findings with no sense of the total
    population checked."""
    gap_type: str  # "diagnostic_settings" | "heartbeat"
    checked_count: int
    covered_count: int
    evaluated_at: str
    skipped_permission_errors: int = 0
    evidence: list = field(default_factory=list)

    def __post_init__(self):
        if self.gap_type not in _VALID_TELEMETRY_GAP_TYPES:
            raise ValueError(
                f"TelemetryCoverageSummary.gap_type {self.gap_type!r} must be one of "
                f"{sorted(_VALID_TELEMETRY_GAP_TYPES)}"
            )
        if self.checked_count < 0:
            raise ValueError("TelemetryCoverageSummary.checked_count must be >= 0")
        if self.covered_count < 0:
            raise ValueError("TelemetryCoverageSummary.covered_count must be >= 0")
        if self.covered_count > self.checked_count:
            raise ValueError("TelemetryCoverageSummary.covered_count cannot exceed checked_count")
        if self.skipped_permission_errors < 0:
            raise ValueError("TelemetryCoverageSummary.skipped_permission_errors must be >= 0")
        self.evaluated_at = ensure_utc_iso(self.evaluated_at, field_name="TelemetryCoverageSummary.evaluated_at")
        for i, item in enumerate(self.evidence):
            if not isinstance(item, EvidenceReference):
                raise TypeError(f"TelemetryCoverageSummary.evidence[{i}] must be an EvidenceReference instance")

    @property
    def coverage_pct(self) -> Optional[float]:
        if self.checked_count == 0:
            return None
        return round(self.covered_count / self.checked_count * 100, 2)

    def to_dict(self) -> dict:
        return {
            "gap_type": self.gap_type,
            "checked_count": self.checked_count,
            "covered_count": self.covered_count,
            "coverage_pct": self.coverage_pct,
            "skipped_permission_errors": self.skipped_permission_errors,
            "evaluated_at": self.evaluated_at,
            "evidence": [item.to_dict() for item in self.evidence],
        }
