"""Bounded, redacted evidence bundles for grounded agent analysis.

``build_evidence_bundle`` is the ONLY way app/agents/analysis.py reads
Findings out of an already-built ``OperationsSnapshot``
(app/operations/snapshot.py) -- it never hands a raw Finding, raw
CollectionEnvelope, or any Azure API payload to a model. Every field
that reaches a model prompt through this module is:

  * bounded    -- at most `max_items` findings, at most 3 evidence
                  references per finding;
  * redacted   -- no `resource_id` (an ARM id whose first path segment
                  is the subscription GUID -- see
                  app.operations.brief.sanitize_evidence, reused here),
                  no `raw_excerpt`, no subscription id, no endpoint URL;
  * summarized -- `coverage` is the same ok/error/not_configured COUNTS
                  app.operations.service.summarize_coverage already
                  produces, never the underlying error text/detail.
"""

from dataclasses import dataclass, field

from app.agents.runner import truncate_context
from app.operations.brief import sanitize_evidence
from app.operations.models import FindingCategory, FindingStatus, Severity
from app.operations.snapshot import OperationsSnapshot
from app.operations.state import WORKFLOW_STATUSES

__all__ = [
    "DEFAULT_MAX_ITEMS",
    "MAX_ITEMS_CEILING",
    "MAX_EVIDENCE_PER_ITEM",
    "EvidenceBundleError",
    "EvidenceBundleItem",
    "EvidenceBundle",
    "build_evidence_bundle",
]

DEFAULT_MAX_ITEMS = 12
MAX_ITEMS_CEILING = 25
MAX_EVIDENCE_PER_ITEM = 3
# Defense-in-depth cap on the serialized bundle handed to a model prompt
# -- bounded item/evidence counts above should already keep this well
# under this limit; this is a hard backstop, not the primary control.
_MAX_PROMPT_CHARS = 24000

_VALID_CATEGORIES = {member.value for member in FindingCategory}
_VALID_SEVERITIES = {member.value for member in Severity}
_VALID_WORKFLOW_STATUSES = set(WORKFLOW_STATUSES)
_VALID_FINDING_STATUSES = {member.value for member in FindingStatus}


class EvidenceBundleError(ValueError):
    """An invalid filter, or an explicitly-requested finding_id that
    isn't present in the current snapshot."""


@dataclass(frozen=True)
class EvidenceBundleItem:
    id: str
    category: str
    severity: str
    title: str
    summary: str
    business_impact: str
    owner: str
    recommended_action: str
    confidence: str
    approval_required: bool
    priority_band: str
    customer_impact: bool
    workflow_status: str
    evidence: tuple = field(default_factory=tuple)  # tuple[dict] -- bounded, redacted references

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "summary": self.summary,
            "business_impact": self.business_impact,
            "owner": self.owner,
            "recommended_action": self.recommended_action,
            "confidence": self.confidence,
            "approval_required": self.approval_required,
            "priority_band": self.priority_band,
            "customer_impact": self.customer_impact,
            "workflow_status": self.workflow_status,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class EvidenceBundle:
    items: tuple  # tuple[EvidenceBundleItem, ...], already priority-ordered
    total_available: int
    truncated: bool
    generated_at: str
    snapshot_id: str
    subscription_count: int
    coverage: dict  # app.operations.service.summarize_coverage() output -- counts only

    def known_ids(self) -> set:
        return {item.id for item in self.items}

    def to_dict(self) -> dict:
        return {
            "items": [item.to_dict() for item in self.items],
            "total_available": self.total_available,
            "truncated": self.truncated,
            "generated_at": self.generated_at,
            "snapshot_id": self.snapshot_id,
            "subscription_count": self.subscription_count,
            "coverage": self.coverage,
        }

    def to_prompt_json(self) -> str:
        """A bounded JSON string suitable for a model prompt -- the exact
        (and only) ground truth an agent may cite finding ids from."""
        import json

        serialized = json.dumps(self.to_dict(), separators=(",", ":"), default=str)
        return truncate_context(serialized, _MAX_PROMPT_CHARS)


def _finding_matches(item: dict, *, category, severity, status) -> bool:
    finding = item["finding"]
    workflow = item["workflow"]
    if category is not None and finding["category"] != category:
        return False
    if severity is not None and finding["severity"] != severity:
        return False
    if status is not None and workflow["status"] != status and finding["status"] != status:
        return False
    return True


def _sanitize_bundle_evidence(evidence: list) -> tuple:
    """Strip subscription-bearing resource_id (via
    app.operations.brief.sanitize_evidence) AND raw_excerpt, then bound
    to MAX_EVIDENCE_PER_ITEM -- stricter than the brief's own
    sanitization, since a model prompt should never need a raw excerpt
    to cite a finding id."""
    bounded = []
    for ref in sanitize_evidence(evidence[:MAX_EVIDENCE_PER_ITEM]):
        bounded.append({
            "source": ref.get("source"),
            "title": ref.get("title"),
            "observed_at": ref.get("observed_at"),
            "reference": ref.get("reference"),
        })
    return tuple(bounded)


def _build_bundle_item(item: dict) -> EvidenceBundleItem:
    finding = item["finding"]
    workflow = item["workflow"]
    priority = item["priority"]
    owner = (workflow.get("assigned_owner") or finding.get("owner") or "").strip()
    return EvidenceBundleItem(
        id=finding["id"],
        category=finding["category"],
        severity=finding["severity"],
        title=finding["title"],
        summary=finding["summary"],
        business_impact=finding["business_impact"],
        owner=owner,
        recommended_action=finding["recommended_action"],
        confidence=finding["confidence"],
        approval_required=finding["approval_required"],
        priority_band=priority["band"],
        customer_impact=bool(priority["factors"]["customer_impact"]),
        workflow_status=workflow["status"],
        evidence=_sanitize_bundle_evidence(finding.get("evidence") or []),
    )


def build_evidence_bundle(
    snapshot: OperationsSnapshot,
    *,
    category: str = None,
    severity: str = None,
    status: str = None,
    finding_id: str = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> EvidenceBundle:
    """Build a bounded, redacted EvidenceBundle from `snapshot.findings`
    (already priority-ordered by app.operations.priority.prioritize_findings).

    Raises EvidenceBundleError on an unrecognized category/severity/
    status filter value, or when `finding_id` is set but not present in
    the snapshot -- never silently ignores a bad filter or returns an
    empty bundle disguised as "the finding wasn't found"."""
    if category is not None and category not in _VALID_CATEGORIES:
        raise EvidenceBundleError(f"category {category!r} must be one of {sorted(_VALID_CATEGORIES)}")
    if severity is not None and severity not in _VALID_SEVERITIES:
        raise EvidenceBundleError(f"severity {severity!r} must be one of {sorted(_VALID_SEVERITIES)}")
    if status is not None and status not in _VALID_WORKFLOW_STATUSES and status not in _VALID_FINDING_STATUSES:
        raise EvidenceBundleError(
            f"status {status!r} is not a recognized workflow status ({sorted(_VALID_WORKFLOW_STATUSES)}) "
            f"or finding status ({sorted(_VALID_FINDING_STATUSES)})"
        )

    try:
        bounded_max_items = max(1, min(int(max_items), MAX_ITEMS_CEILING))
    except (TypeError, ValueError) as exc:
        raise EvidenceBundleError(f"max_items must be an integer, got {max_items!r}") from exc

    if finding_id:
        filtered = [item for item in snapshot.findings if item["finding"]["id"] == finding_id]
        if not filtered:
            raise EvidenceBundleError(f"finding {finding_id!r} not found in the current snapshot")
    else:
        filtered = [
            item for item in snapshot.findings
            if _finding_matches(item, category=category, severity=severity, status=status)
        ]

    total_available = len(filtered)
    bounded = filtered[:bounded_max_items]
    items = tuple(_build_bundle_item(item) for item in bounded)

    return EvidenceBundle(
        items=items,
        total_available=total_available,
        truncated=total_available > len(items),
        generated_at=snapshot.generated_at,
        snapshot_id=snapshot.id,
        subscription_count=len(snapshot.subscription_ids),
        coverage=snapshot.coverage,
    )
