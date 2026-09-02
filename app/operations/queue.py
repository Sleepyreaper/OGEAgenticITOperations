"""Unified operations queue: a filtered, paginated, ranked view over an
OperationsSnapshot's already-prioritized Findings
(app.operations.snapshot.OperationsSnapshot.findings -- each item already
carries {"finding", "workflow", "priority"} from
app.operations.priority.prioritize_findings via the snapshot service).

Ordering is exactly app.operations.priority's deterministic factors:
customer impact, breached/fast-burning SLO, severity, confidence, then
age -- see PriorityFactors. This module does not re-derive or override
that order; it only filters (status/category/severity/owner) and slices
(page/page_size) the already-ranked list, and formats each item for a
queue UI with an explicit, human-readable reason for its rank.
"""

import math
from typing import Optional

from app.operations.models import FindingCategory, FindingStatus, Severity
from app.operations.state import WORKFLOW_STATUSES

__all__ = ["QueueValidationError", "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "build_queue"]

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

_VALID_CATEGORIES = {member.value for member in FindingCategory}
_VALID_SEVERITIES = {member.value for member in Severity}
_VALID_WORKFLOW_STATUSES = set(WORKFLOW_STATUSES)
# Finding.status (the platform's own evidence-state vocabulary) is
# accepted too, distinctly from workflow status -- see
# app.operations.state's module docstring for why these two vocabularies
# are kept separate.
_VALID_FINDING_STATUSES = {member.value for member in FindingStatus}


class QueueValidationError(ValueError):
    """An unrecognized filter value or an out-of-range page/page_size."""


def _matches(item: dict, *, status: Optional[str], category: Optional[str], severity: Optional[str], owner: Optional[str]) -> bool:
    finding = item["finding"]
    workflow = item["workflow"]
    if status is not None and workflow["status"] != status and finding["status"] != status:
        return False
    if category is not None and finding["category"] != category:
        return False
    if severity is not None and finding["severity"] != severity:
        return False
    if owner is not None:
        owner_lower = owner.strip().lower()
        candidates = {(workflow.get("assigned_owner") or "").strip().lower(), (finding.get("owner") or "").strip().lower()}
        if owner_lower not in candidates:
            return False
    return True


def _rank_reason(factors: dict) -> str:
    """A short, deterministic, evidence-derived explanation of why an
    item ranks where it does -- built only from the same
    PriorityFactors every item already carries, never a free-text
    guess."""
    parts = []
    if factors.get("customer_impact"):
        parts.append("customer-impacting")
    slo_state = factors.get("slo_state")
    if slo_state in ("breached", "at_risk"):
        parts.append(f"SLO {slo_state}")
    parts.append(f"severity_rank={factors.get('severity_rank')}")
    parts.append(f"confidence_rank={factors.get('confidence_rank')}")
    parts.append(f"age={factors.get('age_hours')}h")
    return "; ".join(parts)


def _to_queue_item(item: dict, *, rank: int, total: int) -> dict:
    finding = item["finding"]
    workflow = item["workflow"]
    priority = item["priority"]
    evidence = finding.get("evidence") or []
    return {
        "id": finding["id"],
        "rank": rank,
        "rank_of": total,
        "rank_reason": _rank_reason(priority["factors"]),
        "priority_band": priority["band"],
        "priority_factors": priority["factors"],
        "title": finding["title"],
        "category": finding["category"],
        "severity": finding["severity"],
        "confidence": finding["confidence"],
        "first_seen": finding["first_seen"],
        "last_seen": finding["last_seen"],
        "age_hours": priority["factors"]["age_hours"],
        "business_impact": finding["business_impact"],
        "recommended_action": finding["recommended_action"],
        "approval_required": finding["approval_required"],
        "executive_attention": finding["executive_attention"],
        "evidence_count": len(evidence),
        "evidence": evidence[:5],
        "workflow_status": workflow["status"],
        "assigned_owner": workflow["assigned_owner"],
        "disposition_reason": workflow["disposition_reason"],
        "snooze_until": workflow["snooze_until"],
        "workflow_updated_at": workflow["updated_at"],
    }


def build_queue(
    findings: list,
    *,
    status: Optional[str] = None,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    owner: Optional[str] = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """Filter/paginate an already-prioritized findings list (see module
    docstring) into a queue page. Raises QueueValidationError on an
    unrecognized filter value or an invalid page/page_size -- never
    silently ignores a bad filter."""
    if status is not None:
        if status not in _VALID_WORKFLOW_STATUSES and status not in _VALID_FINDING_STATUSES:
            raise QueueValidationError(
                f"status {status!r} is not a recognized workflow status ({sorted(_VALID_WORKFLOW_STATUSES)}) "
                f"or finding status ({sorted(_VALID_FINDING_STATUSES)})"
            )
    if category is not None and category not in _VALID_CATEGORIES:
        raise QueueValidationError(f"category {category!r} must be one of {sorted(_VALID_CATEGORIES)}")
    if severity is not None and severity not in _VALID_SEVERITIES:
        raise QueueValidationError(f"severity {severity!r} must be one of {sorted(_VALID_SEVERITIES)}")
    if page < 1:
        raise QueueValidationError(f"page must be >= 1, got {page}")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise QueueValidationError(f"page_size must be between 1 and {MAX_PAGE_SIZE}, got {page_size}")

    filtered = [
        item for item in findings
        if _matches(item, status=status, category=category, severity=severity, owner=owner)
    ]
    total = len(filtered)
    start = (page - 1) * page_size
    page_items = filtered[start:start + page_size]

    items = [
        _to_queue_item(item, rank=start + offset + 1, total=total)
        for offset, item in enumerate(page_items)
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if page_size else 0,
    }
