"""Change timeline (Activity Log write/delete operations) and its
deterministic correlation with Resource Health degradation events.

Both signals come from the existing Log Analytics helper
(app.azure_data.query_logs) against the AzureActivity table: changes via
the 'Administrative' category, resource health transitions via the
'ResourceHealth' category (Activity Log's native category for Resource
Health state-change events). Correlation is a timestamp/resource-scoped
window match -- never a model guess (see correlate_changes_with_health).
"""

import json as _json
from datetime import timedelta
from typing import Callable, Optional

from app.azure_data import query_logs as default_query_logs
from app.operations.errors import OperationsCollectionError
from app.operations.models import (
    ConfidenceLevel,
    EvidenceReference,
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    Severity,
    ensure_utc_iso,
    parse_utc_iso,
)

__all__ = [
    "get_change_timeline",
    "get_resource_health_events",
    "correlate_changes_with_health",
    "get_failed_change_findings",
]

CHANGE_SOURCE = EvidenceSource.ACTIVITY_LOG.value
HEALTH_SOURCE = EvidenceSource.RESOURCE_HEALTH.value

# (query, workspace_id, timespan) -> list[dict] -- matches
# app.azure_data.query_logs's signature exactly, so the real function is
# a drop-in default and tests can inject a fake with the same shape.
QueryLogsFn = Callable[..., list]

_DEGRADED_STATUSES = {"degraded", "unavailable"}


def _parse_properties(raw) -> dict:
    """AzureActivity's Properties_d column comes back as a JSON string.
    Parsed defensively: malformed/missing properties degrade to an empty
    dict (they're supplementary detail -- cause/title/previous status --
    not required for a health event's core timestamp/resource/status)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return _json.loads(raw)
        except ValueError:
            return {}
    return {}


def get_change_timeline(
    *,
    lookback_hours: int = 24,
    workspace_id: Optional[str] = None,
    query_logs_fn: QueryLogsFn = default_query_logs,
) -> list:
    """Raw (not yet Finding-ized) write/delete change events, newest first."""
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    query = (
        "AzureActivity "
        f"| where TimeGenerated > ago({lookback_hours}h) "
        "| where CategoryValue == 'Administrative' "
        "| where OperationNameValue endswith '/write' or OperationNameValue endswith '/delete' "
        "| project TimeGenerated, OperationNameValue, ActivityStatusValue, ResourceGroup, ResourceId, Caller, CorrelationId "
        "| order by TimeGenerated desc | take 500"
    )
    try:
        rows = query_logs_fn(query, workspace_id, timedelta(hours=lookback_hours))
    except Exception as exc:
        raise OperationsCollectionError(
            CHANGE_SOURCE, "Log Analytics change timeline query failed", detail=str(exc)
        ) from exc

    changes = []
    for row in rows:
        timestamp = row.get("TimeGenerated")
        if timestamp is None:
            continue
        changes.append({
            "timestamp": ensure_utc_iso(timestamp, field_name="AzureActivity.TimeGenerated"),
            "operation": row.get("OperationNameValue") or "",
            "status": row.get("ActivityStatusValue") or "",
            "resource_group": row.get("ResourceGroup") or "",
            "resource_id": row.get("ResourceId") or "",
            "caller": row.get("Caller") or "",
            "correlation_id": row.get("CorrelationId") or "",
        })
    return changes


def get_resource_health_events(
    *,
    lookback_hours: int = 24,
    workspace_id: Optional[str] = None,
    query_logs_fn: QueryLogsFn = default_query_logs,
) -> list:
    """Raw Resource Health transition events, newest first."""
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    query = (
        "AzureActivity "
        f"| where TimeGenerated > ago({lookback_hours}h) "
        "| where CategoryValue == 'ResourceHealth' "
        "| project TimeGenerated, OperationNameValue, ResourceGroup, ResourceId, Properties_d "
        "| order by TimeGenerated desc | take 500"
    )
    try:
        rows = query_logs_fn(query, workspace_id, timedelta(hours=lookback_hours))
    except Exception as exc:
        raise OperationsCollectionError(
            HEALTH_SOURCE, "Log Analytics resource health query failed", detail=str(exc)
        ) from exc

    events = []
    for row in rows:
        timestamp = row.get("TimeGenerated")
        if timestamp is None:
            continue
        props = _parse_properties(row.get("Properties_d"))
        current_status = str(props.get("currentHealthStatus") or props.get("currentHealthStatusName") or "").strip()
        events.append({
            "timestamp": ensure_utc_iso(timestamp, field_name="AzureActivity.TimeGenerated"),
            "operation": row.get("OperationNameValue") or "",
            "resource_group": row.get("ResourceGroup") or "",
            "resource_id": row.get("ResourceId") or "",
            "current_status": current_status,
            "previous_status": str(props.get("previousHealthStatus") or props.get("previousHealthStatusName") or "").strip(),
            "cause": str(props.get("cause") or "").strip(),
            "title": str(props.get("title") or "").strip(),
        })
    return events


def _is_degraded(event: dict) -> bool:
    return event.get("current_status", "").strip().lower() in _DEGRADED_STATUSES


def correlate_changes_with_health(
    changes: list,
    health_events: list,
    *,
    correlation_window_minutes: int = 60,
) -> list:
    """Deterministically correlate changes with degraded/unavailable
    Resource Health events.

    A change is a candidate cause of a health event when it targets the
    SAME resource (falling back to the same resource group when the
    health event carries no resource id) AND occurred within
    `correlation_window_minutes` before the health event's timestamp.
    Pure timestamp/resource matching -- no model involved.
    """
    if correlation_window_minutes <= 0:
        raise ValueError("correlation_window_minutes must be positive")
    window = timedelta(minutes=correlation_window_minutes)

    findings = []
    for event in health_events:
        if not _is_degraded(event):
            continue
        event_time = parse_utc_iso(event["timestamp"])
        event_resource = event.get("resource_id", "")
        event_rg = event.get("resource_group", "")

        matches = []
        for change in changes:
            change_time = parse_utc_iso(change["timestamp"])
            if not (event_time - window <= change_time <= event_time):
                continue
            same_resource = bool(event_resource) and change.get("resource_id") == event_resource
            same_rg_fallback = (not event_resource) and bool(event_rg) and change.get("resource_group") == event_rg
            if same_resource or same_rg_fallback:
                matches.append(change)

        if not matches:
            continue

        status_label = event["current_status"].lower()
        severity = Severity.HIGH if status_label == "unavailable" else Severity.MEDIUM
        resource_id = event_resource or None

        evidence = [EvidenceReference(
            source=HEALTH_SOURCE,
            title=event.get("title") or f"Resource health: {event['current_status']}",
            observed_at=event["timestamp"],
            resource_id=resource_id,
            reference=event.get("operation") or None,
            raw_excerpt=event.get("cause") or None,
        )]
        for change in matches:
            evidence.append(EvidenceReference(
                source=CHANGE_SOURCE,
                title=change.get("operation") or "Azure Activity Log change",
                observed_at=change["timestamp"],
                resource_id=change.get("resource_id") or None,
                reference=change.get("correlation_id") or None,
                raw_excerpt=f"caller={change.get('caller', '')}; status={change.get('status', '')}",
            ))

        findings.append(Finding(
            category=FindingCategory.CHANGE.value,
            severity=severity.value,
            status=FindingStatus.OPEN.value,
            title=f"Change(s) preceded a {status_label} health event",
            summary=(
                f"{len(matches)} write/delete change(s) occurred within {correlation_window_minutes} "
                f"minute(s) before this resource was reported {status_label}."
            ),
            business_impact=f"Resource reported {status_label} -- investigate whether the preceding change(s) caused it.",
            first_seen=min(c["timestamp"] for c in matches),
            last_seen=event["timestamp"],
            source=HEALTH_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1 if resource_id else 0,
            confidence=ConfidenceLevel.CORRELATED.value,
            evidence=evidence,
            recommended_action="Review the listed change(s) for a causal link to the health degradation; roll back if confirmed.",
            approval_required=True,
            executive_attention=severity == Severity.HIGH,
            metadata={"correlation_window_minutes": correlation_window_minutes, "matched_change_count": len(matches)},
            discriminator=f"{event['timestamp']}|{resource_id or event_rg}",
        ))
    return findings


def get_failed_change_findings(changes: list) -> list:
    """One Finding per FAILED write/delete operation.

    Successful changes are retained as timeline/evidence data (see
    get_change_timeline) and folded into correlation Findings above,
    rather than each becoming its own Finding -- hundreds of routine
    successful changes would otherwise drown out actionable signal.
    """
    findings = []
    for change in changes:
        if change.get("status", "").strip().lower() != "failed":
            continue
        resource_id = change.get("resource_id") or None
        target = resource_id or change.get("resource_group") or "an unknown resource"
        findings.append(Finding(
            category=FindingCategory.CHANGE.value,
            severity=Severity.MEDIUM.value,
            status=FindingStatus.OPEN.value,
            title=f"Failed change: {change.get('operation') or 'unknown operation'}",
            summary=f"{change.get('operation', '')} failed for {target}.",
            business_impact="A change did not apply as intended; the resource may be in an unexpected state.",
            first_seen=change["timestamp"],
            last_seen=change["timestamp"],
            source=CHANGE_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1 if resource_id else 0,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=CHANGE_SOURCE,
                title=change.get("operation") or "Azure Activity Log change",
                observed_at=change["timestamp"],
                resource_id=resource_id,
                reference=change.get("correlation_id") or None,
                raw_excerpt=f"caller={change.get('caller', '')}",
            )],
            recommended_action="Review the failure reason in Activity Log and retry or roll back.",
            approval_required=False,
            executive_attention=False,
            metadata={"caller": change.get("caller", "")},
            discriminator=f"{change['timestamp']}|{change.get('correlation_id', '')}",
        ))
    return findings
