"""Azure Update Manager: machines with missing critical/security updates,
and stale/failed patch assessments, normalized to Findings.

Reads Resource Graph's `patchassessmentresources` table (populated by
Update Manager for both Azure VMs and Azure Arc-enabled servers) via
app.operations.collectors.arg.arg_query -- see docs/AZURE_DATA_SOURCES.md
for the exact query/property assumptions, in particular:

  - Microsoft's own published sample queries for this table use LOWERCASE
    classification keys (`availablePatchCountByClassification.critical`/
    `.security`); this module parses that dict case-insensitively in
    Python instead of hard-coding a single casing in KQL, so it is
    resilient either way.
  - Resource Graph itself only retains 7 days of Update Manager
    assessment history (documented by Microsoft), which is why the
    default staleness threshold (`patch_assessment_stale_days`) is 7.
"""

from datetime import datetime, timezone
from typing import Optional

from app.operations.collectors.arg import QueryResourceGraphFn, arg_query, default_query_resource_graph
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

__all__ = ["normalize_patch_assessment", "collect_patch_compliance"]

SOURCE = EvidenceSource.UPDATE_MANAGER.value

_ASSESSMENT_SUFFIX = "/patchassessmentresults/latest"

QUERY = (
    "patchassessmentresources "
    "| where type =~ 'microsoft.compute/virtualmachines/patchassessmentresults' "
    "or type =~ 'microsoft.hybridcompute/machines/patchassessmentresults' "
    "| project id, name, resourceGroup, subscriptionId, location, properties"
)


def _machine_resource_id(assessment_id: str) -> str:
    """Strip the '/patchAssessmentResults/latest' suffix Update Manager
    appends to the underlying VM/Arc machine's ARM resource id."""
    if assessment_id.lower().endswith(_ASSESSMENT_SUFFIX):
        return assessment_id[: -len(_ASSESSMENT_SUFFIX)]
    return assessment_id


def _classification_counts(properties: dict) -> tuple:
    """(critical_count, security_count), read case-insensitively from
    availablePatchCountByClassification -- see module docstring."""
    raw = properties.get("availablePatchCountByClassification") or {}
    if not isinstance(raw, dict):
        return 0, 0
    lowered = {str(k).lower(): v for k, v in raw.items()}
    critical = int(lowered.get("critical") or 0)
    security = int(lowered.get("security") or 0)
    return critical, security


def normalize_patch_assessment(row: dict, *, stale_days: int = 7, now: Optional[datetime] = None) -> list:
    """0-2 Findings for one patchassessmentresources summary row: one for
    missing critical/security updates (if any), one for a stale/errored
    assessment (if applicable). Both can apply independently."""
    now = now or datetime.now(timezone.utc)
    assessment_id = row.get("id") or ""
    if not assessment_id:
        return []
    resource_id = _machine_resource_id(assessment_id)
    properties = row.get("properties") or {}
    os_type = properties.get("osType") or "unknown OS"
    last_modified_raw = properties.get("lastModifiedDateTime")
    error_details = properties.get("errorDetails") or []
    if isinstance(error_details, dict):
        error_details = [error_details]

    findings = []
    critical_count, security_count = _classification_counts(properties)
    evaluated_at = format_utc_iso(now)
    last_modified = ensure_utc_iso(last_modified_raw, field_name="patchassessmentresources.lastModifiedDateTime") if last_modified_raw else evaluated_at

    if critical_count > 0 or security_count > 0:
        severity = Severity.HIGH if critical_count > 0 else Severity.MEDIUM
        findings.append(Finding(
            category=FindingCategory.PATCH.value,
            severity=severity.value,
            status=FindingStatus.OPEN.value,
            title=f"Missing critical/security updates on {resource_id.split('/')[-1]}",
            summary=f"{critical_count} critical and {security_count} security update(s) are missing ({os_type}).",
            business_impact="Unpatched critical/security updates leave this machine exposed to known vulnerabilities.",
            first_seen=last_modified,
            last_seen=last_modified,
            source=SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=SOURCE,
                title=f"Patch assessment: {resource_id.split('/')[-1]}",
                observed_at=last_modified,
                resource_id=resource_id,
                reference=assessment_id,
                raw_excerpt=f"criticalCount={critical_count}, securityCount={security_count}, osType={os_type}",
            )],
            recommended_action="Schedule an Update Manager patch installation covering the missing critical/security updates.",
            approval_required=False,
            executive_attention=critical_count > 0,
            metadata={"critical_count": critical_count, "security_count": security_count, "os_type": os_type},
            discriminator=f"missing-updates|{assessment_id}",
        ))

    has_errors = bool(error_details)
    age_days = (now - parse_utc_iso(last_modified)).total_seconds() / 86400.0
    is_stale = age_days >= stale_days
    if has_errors or is_stale:
        severity = Severity.HIGH if has_errors else Severity.MEDIUM
        reason = "reported errors during assessment" if has_errors else f"has not refreshed in {age_days:.1f}d (threshold: {stale_days}d)"
        findings.append(Finding(
            category=FindingCategory.PATCH.value,
            severity=severity.value,
            status=FindingStatus.OPEN.value,
            title=f"Patch assessment {'failed' if has_errors else 'stale'} on {resource_id.split('/')[-1]}",
            summary=f"The Update Manager patch assessment for this machine {reason}.",
            business_impact="A failed or stale assessment means missing-update data for this machine can no longer be trusted.",
            first_seen=last_modified,
            last_seen=last_modified,
            source=SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value if has_errors else ConfidenceLevel.DERIVED.value,
            evidence=[EvidenceReference(
                source=SOURCE,
                title=f"Patch assessment: {resource_id.split('/')[-1]}",
                observed_at=last_modified,
                resource_id=resource_id,
                reference=assessment_id,
                raw_excerpt=(str(error_details)[:300] if has_errors else f"lastModifiedDateTime={last_modified}"),
            )],
            recommended_action="Verify the machine's Update Manager/agent connectivity and re-run the patch assessment.",
            approval_required=False,
            executive_attention=has_errors,
            metadata={"has_errors": has_errors, "age_days": round(age_days, 1), "os_type": os_type},
            discriminator=f"stale-assessment|{assessment_id}",
        ))

    return findings


def collect_patch_compliance(
    subscription_ids: list,
    *,
    stale_days: int = 7,
    query_fn: QueryResourceGraphFn = default_query_resource_graph,
    now: Optional[datetime] = None,
) -> list:
    """Missing critical/security updates + stale/failed assessment
    Findings for every VM/Arc machine with a patch assessment summary
    row in Resource Graph, across `subscription_ids`."""
    if not subscription_ids:
        raise ValueError("subscription_ids must be a non-empty list")
    if stale_days <= 0:
        raise ValueError("stale_days must be positive")

    rows = arg_query(QUERY, subscription_ids=subscription_ids, source=SOURCE, query_fn=query_fn)
    findings = []
    for row in rows:
        findings.extend(normalize_patch_assessment(row, stale_days=stale_days, now=now))
    return findings
