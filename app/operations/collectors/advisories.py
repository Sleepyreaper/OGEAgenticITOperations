"""Retirement/deprecation advisories: active Azure Service Health
HealthAdvisory events (which Microsoft's own documentation states
include "all upcoming service retirement events"), extended into
actionable Findings with a deadline when Azure publishes one.

Reads Resource Graph's ServiceHealthResources table via
app.operations.collectors.arg.arg_query -- see docs/AZURE_DATA_SOURCES.md
for the exact query and the ImpactMitigationTime "deadline" assumption.

This deliberately does NOT filter to eventSubType == 'Retirement' only:
Microsoft does not publish a fixed, guaranteed-complete enum of
eventSubType values, and under-collecting a legitimate deprecation
notice under a different subtype would be a worse failure mode than
including a few non-retirement health advisories.
"""

from datetime import datetime, timezone
from typing import Optional

from app.operations.collectors.arg import QueryResourceGraphFn, arg_query, default_query_resource_graph
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
    format_utc_iso,
    parse_utc_iso,
)

__all__ = ["normalize_advisory", "collect_retirement_advisories"]

SOURCE = EvidenceSource.SERVICE_HEALTH.value

_DOTNET_UNIX_EPOCH_TICKS = 621355968000000000
_TICKS_PER_SECOND = 10000000

# Deliberately simple: every properties.* field, including the two
# timestamps, is cast with tostring() -- never todatetime(tolong(...)).
# That combination assumes ImpactStartTime/ImpactMitigationTime are
# epoch-millisecond `dynamic` values; in practice they come back from
# Resource Graph as ISO-8601 datetime strings, and tolong() on a
# non-numeric dynamic value is what produced this query's real
# `ParserFailure` (see docs/AZURE_DATA_SOURCES.md). Deferring the actual
# datetime parsing to Python (ensure_utc_iso/parse_utc_iso in
# normalize_advisory below, which already tolerates either shape) is
# both simpler and removes the ARG-side type-coercion risk entirely.
# `Priority` is projected as `advisoryPriority`, never the bare
# `priority` -- live probing against a real subscription proved this
# exact query otherwise succeeds right up until projecting an alias
# literally named `priority`, which Azure Resource Graph's Kusto
# dialect treats as reserved/problematic and rejects with a
# `ParserFailure` (see docs/AZURE_DATA_SOURCES.md). Renaming only the
# ARG-side extend/project alias -- normalize_advisory below reads the
# row by this same `advisoryPriority` key -- avoids the collision
# entirely without changing the Finding's own `metadata.priority`
# output key.
QUERY = (
    "ServiceHealthResources "
    "| where type =~ 'Microsoft.ResourceHealth/events' "
    "| extend eventType = tostring(properties.EventType), status = tostring(properties.Status) "
    "| where eventType == 'HealthAdvisory' and status =~ 'Active' "
    "| extend eventSubType = tostring(properties.EventSubType), advisoryTitle = tostring(properties.Title), "
    "summaryText = tostring(properties.Summary), trackingId = tostring(properties.TrackingId), "
    "advisoryPriority = tostring(properties.Priority), "
    "impactStart = tostring(properties.ImpactStartTime), "
    "mitigationTime = tostring(properties.ImpactMitigationTime) "
    "| project id, name, subscriptionId, eventSubType, advisoryTitle, summaryText, trackingId, advisoryPriority, impactStart, mitigationTime"
)


def _normalize_service_health_time(value, *, field_name: str) -> Optional[str]:
    """Normalize Service Health timestamps returned as ISO strings or .NET ticks."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.isdigit():
        ticks = int(text)
        if ticks < _DOTNET_UNIX_EPOCH_TICKS:
            raise ValueError(f"{field_name}: invalid .NET tick timestamp {text!r}")
        unix_seconds = (ticks - _DOTNET_UNIX_EPOCH_TICKS) / _TICKS_PER_SECOND
        return format_utc_iso(datetime.fromtimestamp(unix_seconds, tz=timezone.utc))
    return ensure_utc_iso(text, field_name=field_name)


def normalize_advisory(row: dict, *, warning_days: int, now: datetime) -> Finding:
    """Normalize one active HealthAdvisory ServiceHealthResources row
    into a Finding. Severity is threshold-derived from how close the
    published deadline (ImpactMitigationTime) is, when one exists;
    advisories with no published deadline are LOW severity (informational
    -- there is nothing to threshold against)."""
    advisory_id = row.get("id") or row.get("trackingId") or row.get("name") or ""
    if not advisory_id:
        raise OperationsCollectionError(SOURCE, "Service Health advisory payload is missing an id")

    title = row.get("advisoryTitle") or row.get("title") or "Azure Service Health advisory"
    summary_text = (row.get("summaryText") or "")[:500]
    event_sub_type = row.get("eventSubType") or ""
    tracking_id = row.get("trackingId") or ""

    deadline_raw = row.get("mitigationTime") or row.get("impactMitigationTime")
    deadline = _normalize_service_health_time(
        deadline_raw, field_name=f"advisory {advisory_id}.impactMitigationTime"
    )
    impact_start_raw = row.get("impactStart") or row.get("impactStartTime")
    impact_start = _normalize_service_health_time(
        impact_start_raw, field_name=f"advisory {advisory_id}.impactStartTime"
    )

    evaluated_at = format_utc_iso(now)
    days_to_deadline = None
    if deadline:
        days_to_deadline = (parse_utc_iso(deadline) - now).total_seconds() / 86400.0
        severity = Severity.HIGH if days_to_deadline <= warning_days else Severity.MEDIUM
        confidence = ConfidenceLevel.DERIVED
    else:
        severity = Severity.LOW
        confidence = ConfidenceLevel.CONFIRMED

    recommended_action = (
        f"Review this advisory and complete any required migration/action before {deadline}."
        if deadline else
        "Review this advisory for required action; Azure has not published a fixed deadline for it yet."
    )
    business_impact = f"{event_sub_type or 'Service Health advisory'} requires action" + (f" before {deadline}." if deadline else ".")

    return Finding(
        category=FindingCategory.COMPLIANCE.value,
        severity=severity.value,
        status=FindingStatus.OPEN.value,
        title=title,
        summary=summary_text or f"{event_sub_type or 'Health advisory'} affecting this subscription.",
        business_impact=business_impact,
        first_seen=evaluated_at,
        last_seen=evaluated_at,
        source=SOURCE,
        confidence=confidence.value,
        evidence=[EvidenceReference(
            source=SOURCE,
            title=title,
            observed_at=evaluated_at,
            reference=tracking_id or advisory_id,
            raw_excerpt=summary_text or f"eventSubType={event_sub_type}",
        )],
        recommended_action=recommended_action,
        approval_required=False,
        executive_attention=severity == Severity.HIGH,
        metadata={
            "event_sub_type": event_sub_type,
            "tracking_id": tracking_id,
            # Metadata's own output key stays "priority" -- only the ARG
            # extend/project alias needed to change (see QUERY's comment
            # above); this reads the renamed "advisoryPriority" row key.
            "priority": row.get("advisoryPriority", ""),
            "deadline": deadline,
            "impact_start": impact_start,
            "days_to_deadline": round(days_to_deadline, 1) if days_to_deadline is not None else None,
        },
        discriminator=tracking_id or advisory_id,
    )


def collect_retirement_advisories(
    subscription_ids: list,
    *,
    warning_days: int = 180,
    query_fn: QueryResourceGraphFn = default_query_resource_graph,
    now: Optional[datetime] = None,
) -> list:
    """Active Service Health HealthAdvisory events (retirements/
    deprecations/required actions) across `subscription_ids`, each
    normalized into a Finding with its deadline when Azure has published
    one."""
    if not subscription_ids:
        raise ValueError("subscription_ids must be a non-empty list")
    if warning_days <= 0:
        raise ValueError("warning_days must be positive")
    now = now or datetime.now(timezone.utc)

    rows = arg_query(QUERY, subscription_ids=subscription_ids, source=SOURCE, query_fn=query_fn)
    return [normalize_advisory(row, warning_days=warning_days, now=now) for row in rows]
