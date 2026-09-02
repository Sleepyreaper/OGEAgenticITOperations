"""Telemetry coverage gaps: resources lacking diagnostic settings, and
resources with no recent Log Analytics heartbeat, normalized to Findings
plus an explicit TelemetryCoverageSummary denominator.

Both checks operate on a CALLER-SUPPLIED, bounded list of resource ids
(e.g. built from OperationsConfig.telemetry_monitored_resource_types +
telemetry_critical_resource_ids -- see app/operations/service.py) --
never "every resource in the subscription". This is deliberate: most
Azure resource types don't support Microsoft.Insights/diagnosticSettings
at all, and there is no Resource Graph query that can answer "does this
resource have a diagnostic setting" in one call (Resource Graph does not
index diagnosticSettings as a queryable resource type -- see
docs/AZURE_DATA_SOURCES.md), so this must be a bounded, explicit,
per-resource ARM REST check.

Diagnostic-settings coverage tolerates per-resource permission/API
failures the same way keyvault.py does: one resource's failure does not
abort the whole run, and is instead surfaced as a coverage gap plus one
aggregate Finding (never one Finding per failed resource, which could be
very noisy for a large monitored-resource list).
"""

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from app.azure_data import query_logs as default_query_logs
from app.operations.collectors.http import (
    CredentialFactory,
    HttpGet,
    arm_get,
    default_credential_factory,
    default_http_get,
)
from app.operations.errors import OperationsCollectionError
from app.operations.models import (
    ConfidenceLevel,
    EvidenceReference,
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    Severity,
    TelemetryCoverageSummary,
    format_utc_iso,
    utc_now,
)

__all__ = [
    "collect_diagnostic_settings_coverage",
    "get_heartbeat_resource_ids",
    "collect_heartbeat_coverage",
]

SOURCE = EvidenceSource.TELEMETRY_COVERAGE.value
DIAGNOSTIC_SETTINGS_API_VERSION = "2021-05-01-preview"

QueryLogsFn = Callable[..., list]


def _resource_name(resource_id: str) -> str:
    return resource_id.rstrip("/").split("/")[-1] if resource_id else "an unknown resource"


def _diagnostic_settings_permission_gap_finding(failed_count: int, total_count: int, *, now: datetime) -> Finding:
    evaluated_at = format_utc_iso(now)
    return Finding(
        category=FindingCategory.TELEMETRY.value,
        severity=Severity.LOW.value,
        status=FindingStatus.OPEN.value,
        title="Cannot check diagnostic settings for some monitored resources",
        summary=f"{failed_count} of {total_count} monitored resources could not be checked for diagnostic settings (permission/API errors).",
        business_impact="Telemetry coverage for these resources is unknown -- they may or may not have diagnostic settings configured.",
        first_seen=evaluated_at,
        last_seen=evaluated_at,
        source=SOURCE,
        confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=[EvidenceReference(
            source=SOURCE,
            title="Diagnostic settings coverage check",
            observed_at=evaluated_at,
            reference="microsoft.insights/diagnosticSettings",
            raw_excerpt=f"failed_count={failed_count}, total_count={total_count}",
        )],
        recommended_action="Grant the collector's identity Reader access on the affected resources and re-run the coverage check.",
        approval_required=False,
        executive_attention=False,
        metadata={"failed_count": failed_count, "total_count": total_count},
        discriminator="diagnostic-settings-permission-gap",
    )


def collect_diagnostic_settings_coverage(
    resource_ids: list,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    now: Optional[datetime] = None,
) -> tuple:
    """Returns (findings, TelemetryCoverageSummary) for whether each
    resource in `resource_ids` has at least one diagnostic setting
    configured (of any kind -- sending to Log Analytics, Storage, or
    Event Hub all count; see docs/AZURE_DATA_SOURCES.md for the
    "any destination" caveat)."""
    if not resource_ids:
        raise ValueError("resource_ids must be a non-empty list")
    now = now or datetime.now(timezone.utc)
    evaluated_at = format_utc_iso(now)

    findings = []
    checked_count = 0
    covered_count = 0
    permission_errors = 0

    for resource_id in resource_ids:
        try:
            body = arm_get(
                f"{resource_id}/providers/microsoft.insights/diagnosticSettings",
                source=SOURCE,
                params={"api-version": DIAGNOSTIC_SETTINGS_API_VERSION},
                credential_factory=credential_factory,
                http_get=http_get,
            )
        except OperationsCollectionError:
            permission_errors += 1
            continue

        checked_count += 1
        settings = body.get("value") or []
        if settings:
            covered_count += 1
            continue

        findings.append(Finding(
            category=FindingCategory.TELEMETRY.value,
            severity=Severity.MEDIUM.value,
            status=FindingStatus.OPEN.value,
            title=f"No diagnostic settings configured: {_resource_name(resource_id)}",
            summary=f"{_resource_name(resource_id)} has no Microsoft.Insights diagnostic settings configured.",
            business_impact="Logs/metrics for this monitored resource are not being exported anywhere -- incidents involving it may be undiagnosable after the fact.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=SOURCE,
                title=f"Diagnostic settings: {_resource_name(resource_id)}",
                observed_at=evaluated_at,
                resource_id=resource_id,
                reference="microsoft.insights/diagnosticSettings",
                raw_excerpt="value=[] (no diagnostic settings)",
            )],
            recommended_action="Configure a diagnostic setting sending this resource's logs/metrics to Log Analytics (or another destination).",
            approval_required=False,
            executive_attention=False,
            metadata={"resource_id": resource_id},
            discriminator=f"no-diagnostic-settings|{resource_id}",
        ))

    if checked_count == 0 and permission_errors > 0:
        raise OperationsCollectionError(
            SOURCE, "failed to check diagnostic settings for any monitored resource",
            detail=f"{permission_errors} permission/API error(s) across {len(resource_ids)} resource(s)",
        )

    if permission_errors:
        findings.append(_diagnostic_settings_permission_gap_finding(permission_errors, len(resource_ids), now=now))

    summary = TelemetryCoverageSummary(
        gap_type="diagnostic_settings",
        checked_count=checked_count,
        covered_count=covered_count,
        skipped_permission_errors=permission_errors,
        evaluated_at=evaluated_at,
    )
    return findings, summary


def get_heartbeat_resource_ids(
    *,
    lookback_hours: int = 24,
    workspace_id: Optional[str] = None,
    query_logs_fn: QueryLogsFn = default_query_logs,
) -> set:
    """Lower-cased ARM resource ids with at least one Heartbeat row
    (Azure Monitor Agent/Log Analytics agent) within `lookback_hours`."""
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    query = (
        "Heartbeat "
        f"| where TimeGenerated > ago({lookback_hours}h) "
        "| where isnotempty(ResourceId) "
        "| distinct ResourceId"
    )
    try:
        rows = query_logs_fn(query, workspace_id, timedelta(hours=lookback_hours))
    except Exception as exc:
        raise OperationsCollectionError(SOURCE, "Log Analytics Heartbeat query failed", detail=str(exc)) from exc
    return {str(row["ResourceId"]).strip().lower() for row in rows if row.get("ResourceId")}


def collect_heartbeat_coverage(
    resource_ids: list,
    *,
    lookback_hours: int = 24,
    workspace_id: Optional[str] = None,
    query_logs_fn: QueryLogsFn = default_query_logs,
    now=None,
) -> tuple:
    """Returns (findings, TelemetryCoverageSummary) for whether each
    resource in `resource_ids` has sent at least one Heartbeat row within
    `lookback_hours`."""
    if not resource_ids:
        raise ValueError("resource_ids must be a non-empty list")
    now = now or utc_now()
    evaluated_at = format_utc_iso(now)

    live_ids = get_heartbeat_resource_ids(lookback_hours=lookback_hours, workspace_id=workspace_id, query_logs_fn=query_logs_fn)

    findings = []
    covered_count = 0
    for resource_id in resource_ids:
        if resource_id.strip().lower() in live_ids:
            covered_count += 1
            continue
        findings.append(Finding(
            category=FindingCategory.TELEMETRY.value,
            severity=Severity.MEDIUM.value,
            status=FindingStatus.OPEN.value,
            title=f"No Log Analytics heartbeat: {_resource_name(resource_id)}",
            summary=f"{_resource_name(resource_id)} has not sent a Heartbeat row in the last {lookback_hours}h.",
            business_impact="This resource's monitoring agent may be stopped, misconfigured, or the resource itself may be unreachable.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=SOURCE,
                title=f"Heartbeat: {_resource_name(resource_id)}",
                observed_at=evaluated_at,
                resource_id=resource_id,
                reference="Heartbeat",
                raw_excerpt=f"no Heartbeat row within {lookback_hours}h",
            )],
            recommended_action="Verify the Azure Monitor Agent/Log Analytics agent is installed and reporting for this resource.",
            approval_required=False,
            executive_attention=False,
            metadata={"resource_id": resource_id, "lookback_hours": lookback_hours},
            discriminator=f"no-heartbeat|{resource_id}",
        ))

    summary = TelemetryCoverageSummary(
        gap_type="heartbeat",
        checked_count=len(resource_ids),
        covered_count=covered_count,
        evaluated_at=evaluated_at,
    )
    return findings, summary
