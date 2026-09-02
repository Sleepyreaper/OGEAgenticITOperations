"""Adapter: converts app/azure_data.py's existing scan signals into the
structured Finding/EvidenceReference evidence model
(app/operations/models.py), so the product-facing snapshot/brief/queue
services (app/operations/snapshot.py, brief.py, queue.py) can treat
"classic" scan signals exactly like Phase 1/2 collector Findings -- same
schema, same deterministic IDs, same CollectionEnvelope wrapping.

Covers the signals app/azure_data.py already collects that Phase 1/2
(app/operations/collectors/*.py, all built on ARM REST/Log Analytics/
Resource Graph directly) do not: degraded/unavailable Resource Health,
active Service Health incidents, dangerous NSG drift, insecure storage
(public blob access), high-impact Azure Advisor recommendations, Azure
Policy non-compliance (summary + items), resource hygiene (orphaned
disks/NSGs, idle App Service Plans, empty subnets), and missing resource
group owners (tagging compliance).

Every `*_findings` function here is a deterministic transform over
already-fetched azure_data.py raw dict/list shapes -- nothing here calls
an LLM or makes a judgment call (see docs/EVIDENCE_MODEL.md). The actual
Azure fetch calls are made by `collect_legacy_envelopes`, which takes
each azure_data function as an injectable parameter (mirroring
collectors/http.py's credential_factory/http_get DI pattern) so tests
never need real Azure credentials.

Dedup note -- app.azure_data.get_service_health_events (used here) and
app.operations.collectors.advisories.collect_retirement_advisories
(Phase 2) both ultimately read the same underlying
Microsoft.ResourceHealth event feed. To avoid surfacing the same
retirement/deprecation advisory twice, `service_health_findings` below
deliberately excludes eventType == 'HealthAdvisory' events (Phase 2's
advisories.py owns those) and only surfaces the general incident/
informational feed here. A second, cheap safety net exists at the
snapshot layer (app.operations.snapshot._dedupe_findings), which merges
by Finding.id regardless of source, so even a coincidental ID collision
from any other source is deduplicated rather than duplicated.
"""

from datetime import datetime, timezone
from typing import Callable, Optional

from app import azure_data
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
)

__all__ = [
    "resource_health_findings",
    "service_health_findings",
    "security_drift_findings",
    "insecure_storage_findings",
    "advisor_findings",
    "policy_compliance_findings",
    "resource_hygiene_findings",
    "ownership_findings",
    "collect_legacy_envelopes",
]

RESOURCE_HEALTH_SOURCE = EvidenceSource.RESOURCE_HEALTH.value
SERVICE_HEALTH_SOURCE = EvidenceSource.SERVICE_HEALTH.value
RESOURCE_GRAPH_SOURCE = EvidenceSource.RESOURCE_GRAPH.value
ADVISOR_SOURCE = EvidenceSource.ADVISOR.value
POLICY_SOURCE = EvidenceSource.POLICY_INSIGHTS.value

_DEGRADED_STATUSES = {"degraded", "unavailable"}

# Dangerous inbound ports app.azure_data.detect_security_drift already
# filters to (see its query) -- labels are for human-readable titles only,
# the underlying filtering/severity decision is unaffected by this map.
_DANGEROUS_PORT_LABELS = {
    "22": "SSH", "3389": "RDP", "445": "SMB", "1433": "SQL Server",
    "3306": "MySQL", "5432": "PostgreSQL", "*": "all ports",
}
# Ports where any-source inbound access is treated as CRITICAL (remote
# shell/desktop access) rather than HIGH (other database/file-share
# ports -- still dangerous, but not as immediately exploitable for a
# full remote foothold).
_CRITICAL_DANGEROUS_PORTS = {"22", "3389", "*"}

# Azure Advisor's `category` field values -> FindingCategory. Advisor's
# own vocabulary (HighAvailability/Security/Cost/Performance/
# OperationalExcellence) doesn't map 1:1 onto FindingCategory, so this is
# an explicit, documented mapping rather than a guess; anything
# unrecognized falls back to "compliance" (a recommendation to review,
# not a hard reliability/security/cost fact).
_ADVISOR_CATEGORY_MAP = {
    "highavailability": FindingCategory.RELIABILITY.value,
    "security": FindingCategory.SECURITY.value,
    "cost": FindingCategory.COST.value,
    "performance": FindingCategory.RELIABILITY.value,
    "operationalexcellence": FindingCategory.COMPLIANCE.value,
}


def _get_any(row: dict, *keys, default=None):
    """Try several candidate dict keys in order. Resource Graph returns
    JSON keys named literally after an unaliased KQL `project` expression
    -- e.g. `project sku.name` yields the literal key "sku.name", not a
    flattened "sku_name" -- but this has proven inconsistent across
    azure-mgmt-resourcegraph SDK/API-version combinations in practice, so
    callers that read an unaliased projection go through this helper
    defensively instead of a single `row.get(...)`."""
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _build_resource_id(subscription_id: str, resource_group: str, resource_type: str, name: str) -> Optional[str]:
    """Best-effort ARM resource id from Resource Graph's typically-split
    name/resourceGroup/type/subscriptionId columns. Returns None (never a
    guessed placeholder) when any required part is missing."""
    if not (subscription_id and resource_group and resource_type and name):
        return None
    return f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/{resource_type}/{name}"


def _safe_iso(value, fallback: str) -> str:
    """Best-effort ISO-8601 normalization: a missing/unparsable timestamp
    falls back to `fallback` (the collection time) rather than raising --
    several azure_data.py signals are current-state snapshots with no
    reliable per-item timestamp of their own."""
    if not value:
        return fallback
    try:
        return ensure_utc_iso(value)
    except ValueError:
        return fallback


# ─── Resource Health (degraded/unavailable) ─────────────────────────────

# Resource Health's `reasonType` property (see
# app.azure_data.get_resource_health_statuses) is Azure's own documented
# way to distinguish an authorized, customer-initiated resource state
# (e.g. an owner stopping/deallocating a VM, which Resource Health then
# reports as "Unavailable" because a powered-off VM can't be health-
# probed) from a genuine platform-detected failure -- see
# docs/AZURE_DATA_SOURCES.md. An authorized stop is never a customer-
# impacting event and never demands executive attention on its own.
_AUTHORIZED_STOP_REASON_TYPES = {"userinitiated"}

# Live gap observed in at least one tenant: `reasonType` comes back
# blank/absent on an "Unavailable" VM even though Resource Health's own
# `title`/`summary` text is Azure's exact, documented wording for an
# authorized stop/deallocate (see docs/AZURE_DATA_SOURCES.md). This is a
# NARROW fallback -- an exact (case-insensitive) full-string match on
# one of these two specific, known Azure-published strings, never a
# loose substring check like "contains 'stopped'" -- so an arbitrary
# Unavailable resource is never misclassified as authorized just
# because its title/summary happens to mention being stopped for some
# other, non-authorized reason. Only consulted when `reasonType` itself
# is absent -- a populated (even unrecognized) `reasonType` always wins
# and this fallback is never consulted.
_AUTHORIZED_STOP_TITLE_EXACT = "stopped and deallocated"
_AUTHORIZED_STOP_SUMMARY_EXACT = (
    "this virtual machine is stopped and deallocated as requested by an authorized user or process."
)


def resource_health_findings(rows: list, *, subscription_id: str, now: Optional[datetime] = None) -> list:
    """Degraded/Unavailable Resource Health Findings from
    app.azure_data.get_resource_health_statuses's shape:
    {name, resourceGroup, type, status, summary, title, location,
    reasonType}.

    Resource Health "Unavailable"/"Degraded" alone is an operational
    risk signal, never definitionally customer impact (see
    app.operations.priority.is_customer_impacting) -- this collector has
    no evidence field confirming actual customer/workload impact, so
    `customer_impacting` is never set True here. An "Unavailable" status
    whose `reasonType` is Azure's documented "UserInitiated" value (an
    authorized stop/deallocate, not a platform failure) is additionally
    downgraded to informational severity with no executive attention --
    it must never inflate reliability risk counts the way an actual
    platform-detected failure does.

    Some tenants have been observed reporting `reasonType` blank/absent
    on an authorized stop/deallocate (a live Resource Health data gap,
    not a documented alternative state) -- when that happens, an EXACT
    (case-insensitive) match of `title`/`summary` against Azure's own
    published authorized-stop wording is used as a narrow fallback so
    the Finding is still correctly downgraded (see
    `_AUTHORIZED_STOP_TITLE_EXACT`/`_AUTHORIZED_STOP_SUMMARY_EXACT`
    above) instead of surfacing as a false HIGH-severity platform
    failure. This never broadens to "any Unavailable status that
    mentions being stopped" -- only these two exact, documented phrases.
    """
    now = now or datetime.now(timezone.utc)
    evaluated_at = format_utc_iso(now)
    findings = []
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        if status not in _DEGRADED_STATUSES:
            continue
        name = row.get("name") or ""
        rg = row.get("resourceGroup") or ""
        rtype = row.get("type") or ""
        resource_id = _build_resource_id(subscription_id, rg, rtype, name)
        reason_type = str(row.get("reasonType") or "").strip().lower()
        title_raw = row.get("title") or ""
        summary_raw = row.get("summary") or ""
        evidence_based_authorized_stop = (
            status == "unavailable"
            and not reason_type
            and (
                title_raw.strip().lower() == _AUTHORIZED_STOP_TITLE_EXACT
                or summary_raw.strip().lower() == _AUTHORIZED_STOP_SUMMARY_EXACT
            )
        )
        authorized_stop = (
            status == "unavailable" and reason_type in _AUTHORIZED_STOP_REASON_TYPES
        ) or evidence_based_authorized_stop
        if authorized_stop:
            severity = Severity.INFORMATIONAL
        else:
            severity = Severity.HIGH if status == "unavailable" else Severity.MEDIUM
        title = title_raw or f"Resource health: {row.get('status')}"
        summary = (summary_raw or f"{name or 'A monitored resource'} is reporting {row.get('status')}.")[:500]
        target = resource_id or name or rg or "a monitored resource"

        business_impact = (
            f"{name or target} was intentionally stopped/deallocated (an authorized action) -- Resource Health "
            "reports it Unavailable as an expected side effect, not a platform failure."
            if authorized_stop else
            f"{name or target} is {row.get('status')} -- dependent workloads may be impacted."
        )
        recommended_action = (
            "No action required -- this is the expected Resource Health status for an intentionally stopped/"
            "deallocated resource."
            if authorized_stop else
            "Review Resource Health details in the Azure portal and investigate the reported cause."
        )

        findings.append(Finding(
            category=FindingCategory.RELIABILITY.value,
            severity=severity.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=summary,
            business_impact=business_impact,
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=RESOURCE_HEALTH_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=RESOURCE_HEALTH_SOURCE,
                title=title,
                observed_at=evaluated_at,
                resource_id=resource_id,
                reference=rtype or None,
                raw_excerpt=summary or None,
            )],
            recommended_action=recommended_action,
            approval_required=False,
            executive_attention=severity == Severity.HIGH,
            customer_impacting=False,
            metadata={
                "resource_group": rg, "resource_type": rtype, "location": row.get("location", ""),
                "subscription_id": subscription_id, "reason_type": row.get("reasonType", ""), "authorized_stop": authorized_stop,
                "authorized_stop_evidence_fallback": evidence_based_authorized_stop,
            },
            discriminator=f"{subscription_id}|{rg}|{name}|{status}",
        ))
    return findings


# ─── Service Health (active incidents) ──────────────────────────────────

# Service Health `eventType` values Azure's own REST API documentation
# defines as an active, unplanned service problem -- "ServiceIssue" is
# the officially documented value (as opposed to PlannedMaintenance/RCA/
# EmergingIssues/SecurityAdvisory, or HealthAdvisory, already excluded
# above); "Incident" is included too since some Azure API generations/
# tenants have been observed to surface this eventType literally. See
# docs/AZURE_DATA_SOURCES.md. Combined with status == 'active' AND this
# event already being scoped to `subscription_id` (Service Health's
# events API only returns events Azure has determined affect the
# queried subscription), this is genuine, deterministic evidence of
# real customer/workload impact -- see Finding.customer_impacting's
# contract in app.operations.models. A resolved incident, or an active
# PlannedMaintenance/RCA/EmergingIssues/SecurityAdvisory event, is real
# but is NOT treated as current customer impact.
_ACTIVE_INCIDENT_EVENT_TYPES = {"serviceissue", "incident"}


def service_health_findings(events: list, *, subscription_id: str, now: Optional[datetime] = None) -> list:
    """Active Azure Service Health incidents from
    app.azure_data.get_service_health_events's shape:
    {title, status, level, eventType, impactStart, services, regions,
    summary}. Deliberately skips eventType == 'HealthAdvisory' -- see
    module docstring's dedup note (Phase 2's advisories.py owns those)."""
    now = now or datetime.now(timezone.utc)
    evaluated_at = format_utc_iso(now)
    findings = []
    for event in events:
        event_type = str(event.get("eventType") or "").strip()
        if event_type.lower() == "healthadvisory":
            continue
        status = str(event.get("status") or "").strip().lower()
        if status != "active":
            continue

        level = str(event.get("level") or "").strip().lower()
        if level in ("critical", "error", "sev0", "sev1"):
            severity = Severity.HIGH
        elif level in ("warning", "sev2"):
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        title = event.get("title") or "Azure Service Health event"
        first_seen = _safe_iso(event.get("impactStart"), evaluated_at)
        services = sorted(set(event.get("services") or []))
        regions = sorted(set(event.get("regions") or []))
        summary = (event.get("summary") or f"Active {event_type or 'Service Health'} event.")[:500]

        business_impact = f"Active Azure Service Health event affecting {', '.join(services) or 'this subscription'}"
        if regions:
            business_impact += f" in {', '.join(regions)}"
        business_impact += "."

        findings.append(Finding(
            category=FindingCategory.INCIDENT.value,
            severity=severity.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=summary,
            business_impact=business_impact,
            first_seen=first_seen,
            last_seen=evaluated_at,
            source=SERVICE_HEALTH_SOURCE,
            affected_resource_count=0,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=SERVICE_HEALTH_SOURCE,
                title=title,
                observed_at=first_seen,
                reference=event_type or None,
                raw_excerpt=summary,
            )],
            recommended_action="Review the Service Health event in the Azure portal for mitigation guidance and affected scope.",
            approval_required=False,
            executive_attention=severity == Severity.HIGH,
            customer_impacting=event_type.lower() in _ACTIVE_INCIDENT_EVENT_TYPES,
            metadata={"event_type": event_type, "level": event.get("level", ""), "services": services, "regions": regions, "subscription_id": subscription_id},
            discriminator=f"{subscription_id}|{title}|{first_seen}|{','.join(services)}",
        ))
    return findings


# ─── Security drift (dangerous NSG rules) ───────────────────────────────

def security_drift_findings(rows: list, *, now: Optional[datetime] = None) -> list:
    """Dangerous inbound NSG rules from
    app.azure_data.detect_security_drift's (already multi-subscription)
    shape: {nsgName, ruleName, port, priority, resourceGroup,
    subscriptionId}."""
    now = now or datetime.now(timezone.utc)
    evaluated_at = format_utc_iso(now)
    findings = []
    for row in rows:
        nsg = row.get("nsgName") or ""
        if not nsg:
            continue
        rule = row.get("ruleName") or ""
        port = str(row.get("port") or "")
        rg = row.get("resourceGroup") or ""
        sub = row.get("subscriptionId") or ""
        port_label = _DANGEROUS_PORT_LABELS.get(port, port or "an unspecified port")
        severity = Severity.CRITICAL if port in _CRITICAL_DANGEROUS_PORTS else Severity.HIGH
        resource_id = _build_resource_id(sub, rg, "Microsoft.Network/networkSecurityGroups", nsg)
        title = f"NSG '{nsg}' allows inbound {port_label} from any source"

        findings.append(Finding(
            category=FindingCategory.SECURITY.value,
            severity=severity.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=f"Rule '{rule}' on NSG '{nsg}' allows inbound traffic on port {port or '?'} from '*' (any source).",
            business_impact=f"Exposes {port_label} to the internet -- a common initial-access vector.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=RESOURCE_GRAPH_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=RESOURCE_GRAPH_SOURCE,
                title=title,
                observed_at=evaluated_at,
                resource_id=resource_id,
                reference=rule or None,
                raw_excerpt=f"priority={row.get('priority')}",
            )],
            recommended_action=f"Restrict the source address prefix for rule '{rule}' or remove public inbound access on port {port or '?'}.",
            approval_required=True,
            executive_attention=severity == Severity.CRITICAL,
            metadata={"nsg_name": nsg, "rule_name": rule, "port": port, "priority": row.get("priority"), "resource_group": rg, "subscription_id": sub},
            discriminator=f"{sub}|{rg}|{nsg}|{rule}|{port}",
        ))
    return findings


# ─── Insecure storage (public blob access) ──────────────────────────────

def insecure_storage_findings(rows: list, *, now: Optional[datetime] = None) -> list:
    """Public-blob-access storage accounts from
    app.azure_data.detect_insecure_storage's (already multi-subscription)
    shape: {name, resourceGroup, location, publicAccess, subscriptionId}."""
    now = now or datetime.now(timezone.utc)
    evaluated_at = format_utc_iso(now)
    findings = []
    for row in rows:
        name = row.get("name") or ""
        if not name:
            continue
        rg = row.get("resourceGroup") or ""
        sub = row.get("subscriptionId") or ""
        resource_id = _build_resource_id(sub, rg, "Microsoft.Storage/storageAccounts", name)
        title = f"Storage account '{name}' allows public blob access"

        findings.append(Finding(
            category=FindingCategory.SECURITY.value,
            severity=Severity.HIGH.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=f"'{name}' has allowBlobPublicAccess enabled.",
            business_impact="Publicly readable/writable blob data is a common data-exposure vector.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=RESOURCE_GRAPH_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=RESOURCE_GRAPH_SOURCE,
                title=title,
                observed_at=evaluated_at,
                resource_id=resource_id,
                raw_excerpt=f"location={row.get('location', '')}",
            )],
            recommended_action="Disable allowBlobPublicAccess unless an explicit, reviewed business need requires it.",
            approval_required=True,
            executive_attention=True,
            metadata={"resource_group": rg, "location": row.get("location", ""), "subscription_id": sub},
            discriminator=f"{sub}|{rg}|{name}",
        ))
    return findings


# ─── Azure Advisor (high-impact only) ───────────────────────────────────

def advisor_findings(recs: list, *, subscription_id: str, now: Optional[datetime] = None) -> list:
    """High-impact-only Azure Advisor recommendations from
    app.azure_data.get_advisor_recommendations's shape:
    {category, impact, problem, solution, resource}. Medium/Low-impact
    recommendations are real but deliberately out of scope for this
    actionable-Findings adapter (mirrors defender.py's high/medium-only
    Defender alert scoping) -- they remain visible in Advisor itself."""
    now = now or datetime.now(timezone.utc)
    evaluated_at = format_utc_iso(now)
    findings = []
    for rec in recs:
        impact = str(rec.get("impact") or "").strip().lower()
        if impact != "high":
            continue
        category_raw = rec.get("category") or "Recommendation"
        resource_id = rec.get("resource") or None
        problem = (rec.get("problem") or "Azure Advisor recommendation").strip()
        solution = (rec.get("solution") or "").strip()
        category = _ADVISOR_CATEGORY_MAP.get(category_raw.lower(), FindingCategory.COMPLIANCE.value)
        title = f"Advisor ({category_raw}): {problem}"[:200]

        findings.append(Finding(
            category=category,
            severity=Severity.HIGH.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=problem[:500],
            business_impact=(f"High-impact Advisor recommendation ({category_raw}) -- {solution}" if solution else f"High-impact Advisor recommendation ({category_raw}).")[:500],
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=ADVISOR_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1 if resource_id else 0,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=ADVISOR_SOURCE,
                title=title,
                observed_at=evaluated_at,
                resource_id=resource_id,
                reference=category_raw,
                raw_excerpt=solution or None,
            )],
            recommended_action=solution or "Review this Azure Advisor recommendation.",
            approval_required=False,
            executive_attention=True,
            metadata={"advisor_category": category_raw, "subscription_id": subscription_id},
            discriminator=f"{subscription_id}|{category_raw}|{resource_id or problem}",
        ))
    return findings


# ─── Azure Policy compliance (summary + non-compliant items) ────────────

def policy_compliance_findings(summary: dict, items: list, *, subscription_id: str, now: Optional[datetime] = None) -> list:
    """One subscription-level summary Finding (only when non-compliant
    resources exist) plus one Finding per non-compliant resource, from
    app.azure_data.get_policy_compliance_summary/get_non_compliant_resources's
    shapes."""
    now = now or datetime.now(timezone.utc)
    evaluated_at = format_utc_iso(now)
    findings = []

    total = summary.get("total_resources", 0) or 0
    non_compliant = summary.get("non_compliant_resources", 0) or 0
    compliance_pct = summary.get("compliance_pct")
    if non_compliant > 0:
        severity = Severity.HIGH if (compliance_pct is not None and compliance_pct < 80) else Severity.MEDIUM
        title = f"Azure Policy: {non_compliant} non-compliant resource(s)"
        findings.append(Finding(
            category=FindingCategory.COMPLIANCE.value,
            severity=severity.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=(
                f"{non_compliant} of {total} resources are non-compliant "
                f"({compliance_pct if compliance_pct is not None else 'unknown'}% compliant) "
                f"across {summary.get('non_compliant_policies', 0)} polic(y/ies)."
            ),
            business_impact="Non-compliant resources may violate governance/regulatory requirements.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=POLICY_SOURCE,
            affected_resource_count=non_compliant,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=POLICY_SOURCE,
                title=title,
                observed_at=evaluated_at,
                raw_excerpt=f"compliance_pct={compliance_pct}",
            )],
            recommended_action="Review non-compliant resources and remediate, or request a documented exemption.",
            approval_required=False,
            executive_attention=severity == Severity.HIGH,
            metadata={"subscription_id": subscription_id, "total_resources": total, "non_compliant_resources": non_compliant, "compliance_pct": compliance_pct},
            discriminator=f"{subscription_id}|policy-summary",
        ))

    for item in items:
        resource_id = item.get("resourceId") or None
        assignment = item.get("policyAssignmentName") or ""
        definition = item.get("policyDefinitionName") or ""
        display_name = item.get("resourceName") or resource_id or "resource"
        title = f"Non-compliant: {display_name} ({definition or assignment or 'policy'})"[:200]

        findings.append(Finding(
            category=FindingCategory.COMPLIANCE.value,
            severity=Severity.MEDIUM.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=f"Resource is non-compliant with policy assignment '{assignment}' (definition '{definition}').",
            business_impact="Individual non-compliant resource -- may violate governance/regulatory requirements.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=POLICY_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1 if resource_id else 0,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=POLICY_SOURCE,
                title=title,
                observed_at=evaluated_at,
                resource_id=resource_id,
                reference=assignment or None,
                raw_excerpt=f"action={item.get('policyDefinitionAction', '')}",
            )],
            recommended_action="Remediate the resource to satisfy the policy definition, or request a documented exemption.",
            approval_required=False,
            executive_attention=False,
            metadata={"subscription_id": subscription_id, "resource_type": item.get("resourceType", ""), "policy_assignment": assignment, "policy_definition": definition},
            discriminator=f"{subscription_id}|{resource_id or display_name}|{assignment}|{definition}",
        ))
    return findings


# ─── Resource hygiene (orphaned disks/NSGs, idle plans, empty subnets) ──

def resource_hygiene_findings(*, orphaned_disks: list, deep_analysis: dict, now: Optional[datetime] = None) -> list:
    """Cost/hygiene Findings from app.azure_data.get_orphaned_disks (a
    top-level, already multi-subscription call) and the
    idle_app_service_plans/orphaned_nsgs/empty_subnets lists inside
    app.azure_data.get_deep_analysis's (also multi-subscription) dict."""
    now = now or datetime.now(timezone.utc)
    evaluated_at = format_utc_iso(now)
    findings = []

    for disk in orphaned_disks:
        name = disk.get("name") or ""
        if not name:
            continue
        rg = disk.get("resourceGroup") or ""
        sub = disk.get("subscriptionId") or ""
        size_gb = _get_any(disk, "properties.diskSizeGB", "properties_diskSizeGB", "diskSizeGB")
        resource_id = _build_resource_id(sub, rg, "Microsoft.Compute/disks", name)
        title = f"Orphaned disk: {name}"
        findings.append(Finding(
            category=FindingCategory.COST.value,
            severity=Severity.LOW.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=f"Disk '{name}' is not attached to any VM" + (f" ({size_gb} GB)" if size_gb else "") + ".",
            business_impact="Unattached disks continue to incur storage cost with no active workload.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=RESOURCE_GRAPH_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(
                source=RESOURCE_GRAPH_SOURCE, title=title, observed_at=evaluated_at,
                resource_id=resource_id, raw_excerpt=f"size_gb={size_gb}",
            )],
            recommended_action="Delete the disk if no longer needed, or attach it/tag it as an intentional standby.",
            approval_required=True,
            executive_attention=False,
            metadata={"resource_group": rg, "subscription_id": sub, "disk_size_gb": size_gb},
            discriminator=f"{sub}|{rg}|{name}|disk",
        ))

    for nsg in deep_analysis.get("orphaned_nsgs", []) or []:
        name = nsg.get("name") or ""
        if not name:
            continue
        rg = nsg.get("resourceGroup") or ""
        sub = nsg.get("subscriptionId") or ""
        resource_id = _build_resource_id(sub, rg, "Microsoft.Network/networkSecurityGroups", name)
        title = f"Orphaned NSG: {name}"
        findings.append(Finding(
            category=FindingCategory.COST.value,
            severity=Severity.LOW.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=f"NSG '{name}' is attached to no subnets and no network interfaces.",
            business_impact="Unattached NSGs are dead configuration -- a source of audit/architecture confusion, not active cost, but worth cleaning up.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=RESOURCE_GRAPH_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(source=RESOURCE_GRAPH_SOURCE, title=title, observed_at=evaluated_at, resource_id=resource_id)],
            recommended_action="Delete the NSG if it is no longer needed, or attach it to its intended subnet/NIC.",
            approval_required=True,
            executive_attention=False,
            metadata={"resource_group": rg, "subscription_id": sub},
            discriminator=f"{sub}|{rg}|{name}|nsg",
        ))

    for plan in deep_analysis.get("idle_app_service_plans", []) or []:
        name = plan.get("name") or ""
        if not name:
            continue
        rg = plan.get("resourceGroup") or ""
        sub = plan.get("subscriptionId") or ""
        tier = plan.get("tier") or plan.get("sku") or ""
        resource_id = _build_resource_id(sub, rg, "Microsoft.Web/serverfarms", name)
        title = f"Idle App Service Plan: {name}"
        findings.append(Finding(
            category=FindingCategory.COST.value,
            severity=Severity.LOW.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=f"App Service Plan '{name}'" + (f" ({tier})" if tier else "") + " has zero hosted apps.",
            business_impact="An idle App Service Plan continues to incur compute cost with nothing running on it.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=RESOURCE_GRAPH_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(source=RESOURCE_GRAPH_SOURCE, title=title, observed_at=evaluated_at, resource_id=resource_id, raw_excerpt=f"tier={tier}")],
            recommended_action="Delete the plan if no longer needed, or move it out of an isolated/reserved SKU it no longer justifies.",
            approval_required=True,
            executive_attention=False,
            metadata={"resource_group": rg, "subscription_id": sub, "tier": tier},
            discriminator=f"{sub}|{rg}|{name}|plan",
        ))

    for subnet in deep_analysis.get("empty_subnets", []) or []:
        vnet_name = subnet.get("vnetName") or ""
        subnet_name = subnet.get("subnetName") or ""
        if not (vnet_name and subnet_name):
            continue
        rg = subnet.get("resourceGroup") or ""
        sub = subnet.get("subscriptionId") or ""
        vnet_resource_id = _build_resource_id(sub, rg, "Microsoft.Network/virtualNetworks", vnet_name)
        resource_id = f"{vnet_resource_id}/subnets/{subnet_name}" if vnet_resource_id else None
        title = f"Empty subnet: {vnet_name}/{subnet_name}"
        findings.append(Finding(
            category=FindingCategory.COST.value,
            severity=Severity.INFORMATIONAL.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=f"Subnet '{subnet_name}' in VNet '{vnet_name}' has no connected devices or delegations.",
            business_impact="Allocated address space with nothing using it -- worth reclaiming during capacity planning.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=RESOURCE_GRAPH_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(source=RESOURCE_GRAPH_SOURCE, title=title, observed_at=evaluated_at, resource_id=resource_id, raw_excerpt=f"address_prefix={subnet.get('addressPrefix', '')}")],
            recommended_action="Reclaim the subnet's address space or document it as reserved for planned future use.",
            approval_required=False,
            executive_attention=False,
            metadata={"resource_group": rg, "subscription_id": sub, "vnet_name": vnet_name, "subnet_name": subnet_name},
            discriminator=f"{sub}|{rg}|{vnet_name}|{subnet_name}",
        ))

    return findings


# ─── Ownership (missing support-owner tag) ──────────────────────────────

def ownership_findings(tagging_rows: list, *, now: Optional[datetime] = None) -> list:
    """Resource groups missing a 'support-owner' tag, from
    app.azure_data.get_tagging_compliance's shape: {name, supportOwner,
    location, tags, subscriptionId}."""
    now = now or datetime.now(timezone.utc)
    evaluated_at = format_utc_iso(now)
    findings = []
    for row in tagging_rows:
        if row.get("supportOwner"):
            continue
        rg_name = row.get("name") or ""
        if not rg_name:
            continue
        sub = row.get("subscriptionId") or ""
        resource_id = (
            f"/subscriptions/{sub}/resourceGroups/{rg_name}" if sub else None
        )
        title = f"Resource group '{rg_name}' has no support-owner tag"
        findings.append(Finding(
            category=FindingCategory.OWNERSHIP.value,
            severity=Severity.LOW.value,
            status=FindingStatus.OPEN.value,
            title=title,
            summary=f"Resource group '{rg_name}' is missing the 'support-owner' tag.",
            business_impact="No clear accountable owner for incident routing/change approval in this resource group.",
            first_seen=evaluated_at,
            last_seen=evaluated_at,
            source=RESOURCE_GRAPH_SOURCE,
            resource_id=resource_id,
            affected_resource_count=1,
            confidence=ConfidenceLevel.CONFIRMED.value,
            evidence=[EvidenceReference(source=RESOURCE_GRAPH_SOURCE, title=title, observed_at=evaluated_at, resource_id=resource_id, raw_excerpt=f"location={row.get('location', '')}")],
            recommended_action="Add a 'support-owner' tag identifying the accountable team/individual for this resource group.",
            approval_required=False,
            executive_attention=False,
            metadata={"resource_group": rg_name, "subscription_id": sub},
            discriminator=f"{sub}|{rg_name}|ownership",
        ))
    return findings


# ─── Orchestration: one CollectionEnvelope-shaped dict per legacy source ─

# (subscription_id) -> raw rows/dict, matching each azure_data.py
# function's real signature exactly, so the real function is a drop-in
# default and tests can inject a fake with the same shape.
ResourceHealthFn = Callable[[str], list]
ServiceHealthFn = Callable[..., list]
SecurityDriftFn = Callable[..., list]
InsecureStorageFn = Callable[..., list]
AdvisorFn = Callable[[str], list]
PolicySummaryFn = Callable[[str], dict]
NonCompliantFn = Callable[..., list]
OrphanedDisksFn = Callable[..., list]
DeepAnalysisFn = Callable[..., dict]
TaggingFn = Callable[..., list]


def _envelope(source: str, *, findings: list = None, status: str = "ok", error: str = None, now: Optional[datetime] = None) -> dict:
    """A CollectionEnvelope-shaped dict -- kept as a plain dict here (not
    importing app.operations.service.CollectionEnvelope) to avoid a
    circular import (service.py is the natural place to eventually fold
    this in; app.operations.snapshot converts these into real
    CollectionEnvelope instances)."""
    now = now or datetime.now(timezone.utc)
    return {
        "source": source,
        "status": status,
        "collected_at": format_utc_iso(now),
        "findings": findings or [],
        "summaries": [],
        "error": error,
    }


def collect_legacy_envelopes(
    subscription_ids: list,
    *,
    resource_health_fn: ResourceHealthFn = azure_data.get_resource_health_statuses,
    service_health_fn: ServiceHealthFn = azure_data.get_service_health_events,
    security_drift_fn: SecurityDriftFn = azure_data.detect_security_drift,
    insecure_storage_fn: InsecureStorageFn = azure_data.detect_insecure_storage,
    advisor_fn: AdvisorFn = azure_data.get_advisor_recommendations,
    policy_summary_fn: PolicySummaryFn = azure_data.get_policy_compliance_summary,
    non_compliant_fn: NonCompliantFn = azure_data.get_non_compliant_resources,
    orphaned_disks_fn: OrphanedDisksFn = azure_data.get_orphaned_disks,
    deep_analysis_fn: DeepAnalysisFn = azure_data.get_deep_analysis,
    tagging_fn: TaggingFn = azure_data.get_tagging_compliance,
    now: Optional[datetime] = None,
) -> list:
    """Run every legacy-scan signal for `subscription_ids` and return one
    CollectionEnvelope-shaped dict per source (8, always in this fixed
    order): legacy_resource_health, legacy_service_health,
    legacy_security_drift, legacy_insecure_storage, legacy_advisor,
    legacy_policy_compliance, legacy_resource_hygiene, legacy_ownership.

    A failure in one source's underlying azure_data.py call is caught and
    turned into that source's own 'error' envelope (never a raised
    exception, and never allowed to blank out another source's results)
    -- the same never-a-single-outage-erases-everything contract
    app.operations.service.run_collection/run_full_collection make for
    Phase 1/2.

    Each `try` block below deliberately catches the broad `Exception`
    (azure_data.py's underlying calls surface a mix of
    requests.RequestException, azure.core exceptions, and plain
    ValueError/KeyError -- there is no single common base type to catch
    narrowly) but NEVER swallows it: it is always immediately re-surfaced
    as that source's own explicit 'error' envelope, exactly the same
    broad-but-explicit precedent app.operations.collectors.http.scoped_get
    documents for Azure AD token acquisition failures.
    """
    if not subscription_ids:
        raise ValueError("subscription_ids must be a non-empty list")
    now = now or datetime.now(timezone.utc)
    envelopes = []

    # legacy_resource_health / legacy_service_health / legacy_advisor /
    # legacy_policy_compliance: azure_data's underlying functions are
    # single-subscription, so these loop and aggregate raw rows first.
    try:
        findings = []
        for sub in subscription_ids:
            findings.extend(resource_health_findings(resource_health_fn(sub), subscription_id=sub, now=now))
    except Exception as exc:  # broad-but-explicit, see docstring above
        envelopes.append(_envelope("legacy_resource_health", status="error", error=str(exc), now=now))
    else:
        envelopes.append(_envelope("legacy_resource_health", findings=findings, now=now))

    try:
        findings = []
        for sub in subscription_ids:
            findings.extend(service_health_findings(service_health_fn(sub, days=30), subscription_id=sub, now=now))
    except Exception as exc:  # broad-but-explicit, see docstring above
        envelopes.append(_envelope("legacy_service_health", status="error", error=str(exc), now=now))
    else:
        envelopes.append(_envelope("legacy_service_health", findings=findings, now=now))

    try:
        rows = security_drift_fn(subscription_ids=list(subscription_ids))
        findings = security_drift_findings(rows, now=now)
    except Exception as exc:  # broad-but-explicit, see docstring above
        envelopes.append(_envelope("legacy_security_drift", status="error", error=str(exc), now=now))
    else:
        envelopes.append(_envelope("legacy_security_drift", findings=findings, now=now))

    try:
        rows = insecure_storage_fn(subscription_ids=list(subscription_ids))
        findings = insecure_storage_findings(rows, now=now)
    except Exception as exc:  # broad-but-explicit, see docstring above
        envelopes.append(_envelope("legacy_insecure_storage", status="error", error=str(exc), now=now))
    else:
        envelopes.append(_envelope("legacy_insecure_storage", findings=findings, now=now))

    try:
        findings = []
        for sub in subscription_ids:
            findings.extend(advisor_findings(advisor_fn(sub), subscription_id=sub, now=now))
    except Exception as exc:  # broad-but-explicit, see docstring above
        envelopes.append(_envelope("legacy_advisor", status="error", error=str(exc), now=now))
    else:
        envelopes.append(_envelope("legacy_advisor", findings=findings, now=now))

    try:
        findings = []
        for sub in subscription_ids:
            summary = policy_summary_fn(sub)
            items = non_compliant_fn(sub)
            findings.extend(policy_compliance_findings(summary, items, subscription_id=sub, now=now))
    except Exception as exc:  # broad-but-explicit, see docstring above
        envelopes.append(_envelope("legacy_policy_compliance", status="error", error=str(exc), now=now))
    else:
        envelopes.append(_envelope("legacy_policy_compliance", findings=findings, now=now))

    try:
        orphaned_disks = orphaned_disks_fn(subscription_ids=list(subscription_ids))
        deep_analysis = deep_analysis_fn(subscription_ids=list(subscription_ids))
        findings = resource_hygiene_findings(orphaned_disks=orphaned_disks, deep_analysis=deep_analysis, now=now)
    except Exception as exc:  # broad-but-explicit, see docstring above
        envelopes.append(_envelope("legacy_resource_hygiene", status="error", error=str(exc), now=now))
    else:
        envelopes.append(_envelope("legacy_resource_hygiene", findings=findings, now=now))

    try:
        tagging_rows = tagging_fn(subscription_ids=list(subscription_ids))
        findings = ownership_findings(tagging_rows, now=now)
    except Exception as exc:  # broad-but-explicit, see docstring above
        envelopes.append(_envelope("legacy_ownership", status="error", error=str(exc), now=now))
    else:
        envelopes.append(_envelope("legacy_ownership", findings=findings, now=now))

    return envelopes
