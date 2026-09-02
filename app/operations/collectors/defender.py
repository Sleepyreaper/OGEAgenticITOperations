"""Microsoft Defender for Cloud: active high/medium alerts and unhealthy
assessments, normalized to Findings.

Uses the subscription-scoped Microsoft.Security REST APIs directly (ARM
REST via app.operations.collectors.http.paginated_get) rather than
Resource
Graph's SecurityResources table: the REST APIs have a stable, documented
camelCase JSON shape (see docs/AZURE_DATA_SOURCES.md), whereas ARG's
serialization of the same underlying payloads has shown inconsistent
property casing across API generations in Microsoft's own published
samples.

Both list APIs' `nextLink` is followed (bounded -- see paginated_get)
rather than silently returning only the first page.

Alerts and assessments are normalized into two DELIBERATELY DISTINCT
Finding categories -- never merged into one aggregate "score":
  - collect_active_alerts()       -> FindingCategory.SECURITY
    (an actual triggered/active threat detection -- an incident-shaped
    fact, confirmed by the platform)
  - collect_unhealthy_assessments() -> FindingCategory.COMPLIANCE
    (a posture/recommendation gap -- one of Secure Score's constituent
    parts. This module never re-aggregates assessments into an invented
    score; it only ever surfaces individual Unhealthy recommendations as
    Findings.)
"""

from typing import Callable, Optional

from app.operations.collectors.http import (
    CredentialFactory,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_RECORDS,
    HttpGet,
    default_credential_factory,
    default_http_get,
    paginated_get,
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
    ensure_utc_iso,
    parse_utc_iso,
    utc_now_iso,
)

__all__ = [
    "normalize_alert",
    "collect_active_alerts",
    "normalize_assessment",
    "collect_unhealthy_assessments",
]

ALERT_SOURCE = EvidenceSource.DEFENDER_ALERT.value
ASSESSMENT_SOURCE = EvidenceSource.DEFENDER_ASSESSMENT.value

ALERTS_API_VERSION = "2022-01-01"
ASSESSMENTS_API_VERSION = "2020-01-01"

_SEVERITY_MAP = {
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "informational": Severity.INFORMATIONAL,
}
# Spec: "active high/medium alerts" -- Low/Informational Defender alerts
# are real but are deliberately out of scope for this actionable-Findings
# collector (they remain visible in the Defender for Cloud portal itself).
_ACTIVE_ALERT_SEVERITIES = {"high", "medium"}


def _severity_from_raw(raw: Optional[str], *, source: str, context: str) -> Severity:
    key = (raw or "").strip().lower()
    if key not in _SEVERITY_MAP:
        raise OperationsCollectionError(source, f"{context}: unrecognized severity {raw!r}")
    return _SEVERITY_MAP[key]


def _assessment_severity_from_raw(raw: Optional[str]) -> "tuple[Severity, bool]":
    """Posture-assessment-only severity resolution -- DELIBERATELY more
    lenient than `_severity_from_raw` above (which stays strict for
    active alerts; see collect_active_alerts/normalize_alert, and
    `_severity_from_raw`'s own test coverage, which is unchanged).

    Some assessment types/tenants have been observed returning
    `metadata.severity` missing/None or an unrecognized value entirely
    (as opposed to an active alert, which always carries a real
    platform-assigned severity). Raising OperationsCollectionError for
    that -- as this collector used to -- aborts the ENTIRE
    defender_assessments source (including every other, perfectly valid
    assessment already collected in the same/earlier page), which is
    disproportionate for what is, at worst, a posture recommendation
    Azure itself didn't rate. Returns (severity, severity_unknown):
    a missing/unrecognized severity maps to INFORMATIONAL with
    severity_unknown=True -- never a guessed/invented HIGH -- so it can
    never demand executive attention or inflate priority on its own;
    a recognized value maps through exactly like `_severity_from_raw`
    with severity_unknown=False.
    """
    key = (raw or "").strip().lower()
    mapped = _SEVERITY_MAP.get(key)
    if mapped is None:
        return Severity.INFORMATIONAL, True
    return mapped, False


def _first_resource_id(resource_identifiers: list) -> Optional[str]:
    for identifier in resource_identifiers or []:
        if not isinstance(identifier, dict):
            continue
        candidate = identifier.get("azureResourceId") or identifier.get("AzureResourceId")
        if candidate:
            return candidate
    return None


def normalize_alert(raw_alert: dict) -> Finding:
    """Normalize one Microsoft.Security/alerts payload (an ACTIVE,
    high/medium-severity alert -- callers filter before calling this;
    see collect_active_alerts) into a Finding."""
    props = raw_alert.get("properties") or {}
    alert_id = raw_alert.get("id") or raw_alert.get("name") or ""
    if not alert_id:
        raise OperationsCollectionError(ALERT_SOURCE, "Defender alert payload is missing an id")

    severity = _severity_from_raw(props.get("severity"), source=ALERT_SOURCE, context=f"alert {alert_id}")

    start_raw = props.get("startTimeUtc") or props.get("timeGeneratedUtc")
    if not start_raw:
        raise OperationsCollectionError(ALERT_SOURCE, f"alert {alert_id} is missing startTimeUtc")
    first_seen = ensure_utc_iso(start_raw, field_name=f"alert {alert_id}.startTimeUtc")
    end_raw = props.get("endTimeUtc") or props.get("timeGeneratedUtc") or start_raw
    last_seen = ensure_utc_iso(end_raw, field_name=f"alert {alert_id}.endTimeUtc")
    if parse_utc_iso(last_seen) < parse_utc_iso(first_seen):
        last_seen = first_seen  # tolerate minor clock skew, as alerts.py does for Azure Monitor alerts

    resource_id = _first_resource_id(props.get("resourceIdentifiers"))
    compromised_entity = props.get("compromisedEntity") or ""
    alert_type = props.get("alertType") or ""
    system_alert_id = props.get("systemAlertId") or alert_id
    display_name = props.get("alertDisplayName") or raw_alert.get("name") or "Microsoft Defender for Cloud alert"
    description = props.get("description") or ""

    remediation_steps = [str(step) for step in (props.get("remediationSteps") or []) if step]
    recommended_action = " ".join(remediation_steps).strip() or "Investigate this Microsoft Defender for Cloud alert."

    target = compromised_entity or resource_id or "a monitored resource"
    evidence = [EvidenceReference(
        source=ALERT_SOURCE,
        title=display_name,
        observed_at=first_seen,
        resource_id=resource_id,
        reference=system_alert_id,
        raw_excerpt=description or f"{alert_type} on {target}",
    )]

    return Finding(
        category=FindingCategory.SECURITY.value,
        severity=severity.value,
        status=FindingStatus.OPEN.value,
        title=display_name,
        summary=description or f"{alert_type or 'Defender for Cloud'} alert detected on {target}.",
        business_impact=f"Active {severity.value}-severity Microsoft Defender for Cloud alert ({alert_type or 'unclassified'}) on {target}.",
        first_seen=first_seen,
        last_seen=last_seen,
        source=ALERT_SOURCE,
        resource_id=resource_id,
        affected_resource_count=1 if resource_id else 0,
        confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=evidence,
        recommended_action=recommended_action[:500],
        approval_required=False,
        executive_attention=severity == Severity.HIGH,
        metadata={
            "alert_type": alert_type,
            "vendor_name": props.get("vendorName", ""),
            "system_alert_id": system_alert_id,
            "compromised_entity": compromised_entity,
        },
        discriminator=system_alert_id,
    )


def collect_active_alerts(
    subscription_id: str,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> list:
    """Active (Microsoft.Security 'status' == Active), high/medium-
    severity Defender for Cloud alerts for one subscription.

    `max_pages`/`max_records` bound how many `nextLink` pages/alerts this
    call will ever follow/accumulate (see
    app.operations.collectors.http.paginated_get)."""
    if not subscription_id:
        raise ValueError("subscription_id is required")

    paged = paginated_get(
        f"/subscriptions/{subscription_id}/providers/Microsoft.Security/alerts",
        source=ALERT_SOURCE,
        params={"api-version": ALERTS_API_VERSION},
        credential_factory=credential_factory,
        http_get=http_get,
        max_pages=max_pages,
        max_records=max_records,
    )

    findings = []
    for raw_alert in paged.items:
        props = raw_alert.get("properties") or {}
        status = (props.get("status") or "").strip().lower()
        severity_raw = (props.get("severity") or "").strip().lower()
        if status != "active" or severity_raw not in _ACTIVE_ALERT_SEVERITIES:
            continue
        findings.append(normalize_alert(raw_alert))
    return findings


def normalize_assessment(raw_assessment: dict, *, now: Optional[str] = None) -> Finding:
    """Normalize one Microsoft.Security/assessments payload (an UNHEALTHY
    assessment -- callers filter before calling this; see
    collect_unhealthy_assessments) into a Finding.

    Assessments describe a live posture STATE, not an event with its own
    timestamp -- the assessments List API carries no "since when has this
    been Unhealthy" field. `first_seen`/`last_seen` are therefore both set
    to the collection time (`now`), matching how capacity.py's
    CapacitySummary treats ARM's equally timestamp-less "usages" API.

    `metadata.severity` missing/unrecognized (observed in some tenants/
    assessment types) NEVER raises here -- see
    `_assessment_severity_from_raw` -- it downgrades to INFORMATIONAL
    with `metadata.severity_unknown=True` on the resulting Finding
    instead, so one assessment with no platform-assigned severity can
    never abort collection of every other assessment in the same
    source (unlike an active alert -- see `normalize_alert`, whose
    severity handling is deliberately unchanged/still strict).
    """
    props = raw_assessment.get("properties") or {}
    assessment_id = raw_assessment.get("id") or raw_assessment.get("name") or ""
    if not assessment_id:
        raise OperationsCollectionError(ASSESSMENT_SOURCE, "Defender assessment payload is missing an id")

    status_obj = props.get("status") or {}
    metadata_obj = props.get("metadata") or {}
    severity, severity_unknown = _assessment_severity_from_raw(metadata_obj.get("severity"))

    display_name = props.get("displayName") or "Microsoft Defender for Cloud recommendation"
    resource_details = props.get("resourceDetails") or {}
    resource_id = resource_details.get("id") or None
    status_description = status_obj.get("description") or ""
    description = metadata_obj.get("description") or status_description
    remediation = metadata_obj.get("remediationDescription") or ""
    categories = metadata_obj.get("categories") or []
    if isinstance(categories, str):
        categories = [categories]

    evaluated_at = now or utc_now_iso()
    target = resource_id or "a monitored resource"

    evidence = [EvidenceReference(
        source=ASSESSMENT_SOURCE,
        title=display_name,
        observed_at=evaluated_at,
        resource_id=resource_id,
        reference=assessment_id,
        raw_excerpt=description or f"Unhealthy: {display_name} on {target}",
    )]

    return Finding(
        category=FindingCategory.COMPLIANCE.value,
        severity=severity.value,
        status=FindingStatus.OPEN.value,
        title=display_name,
        summary=f"{display_name} is Unhealthy for {target}." + (f" {status_description}" if status_description else ""),
        business_impact=f"Security posture gap ({severity.value}) identified by Microsoft Defender for Cloud" + (
            f" ({', '.join(categories)})" if categories else ""
        ) + ".",
        first_seen=evaluated_at,
        last_seen=evaluated_at,
        source=ASSESSMENT_SOURCE,
        resource_id=resource_id,
        affected_resource_count=1 if resource_id else 0,
        confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=evidence,
        recommended_action=(remediation or "Remediate this Microsoft Defender for Cloud recommendation.")[:500],
        approval_required=False,
        # severity_unknown is guaranteed non-HIGH (INFORMATIONAL) already,
        # but spelled out explicitly here too so a future severity
        # mapping change can never accidentally reintroduce executive
        # attention for an assessment Azure never actually rated.
        executive_attention=severity == Severity.HIGH and not severity_unknown,
        metadata={
            "categories": categories, "status_cause": status_obj.get("cause", ""),
            "assessment_name": raw_assessment.get("name", ""), "severity_unknown": severity_unknown,
        },
        discriminator=assessment_id,
    )


def collect_unhealthy_assessments(
    subscription_id: str,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    now: Optional[str] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
    on_partial_result: Optional[Callable[[str], None]] = None,
) -> list:
    """Unhealthy (status.code == 'Unhealthy') Defender for Cloud
    assessments (posture recommendations) for one subscription.

    `max_pages`/`max_records` bound how many `nextLink` pages/assessments
    this call will ever follow/accumulate (see
    app.operations.collectors.http.paginated_get).

    `on_partial_result` (optional) is called with `paged.partial_error`'s
    message when a LATER page (not the first) failed to fetch --
    `paginated_get` already returns the items successfully collected
    from earlier pages rather than raising/discarding them (see its own
    docstring); this callback lets a caller (e.g.
    app.operations.service.collect_defender_assessments_envelope)
    surface that as an explicit coverage warning on the source's
    envelope instead of it being silently dropped. Never called on a
    bound-only truncation (max_pages/max_records reached with no
    fetch failure) -- that case is already visible via
    PagedListResult.truncated/the paginated_get warning log, not a
    collection problem.
    """
    if not subscription_id:
        raise ValueError("subscription_id is required")

    paged = paginated_get(
        f"/subscriptions/{subscription_id}/providers/Microsoft.Security/assessments",
        source=ASSESSMENT_SOURCE,
        params={"api-version": ASSESSMENTS_API_VERSION},
        credential_factory=credential_factory,
        http_get=http_get,
        max_pages=max_pages,
        max_records=max_records,
    )
    if paged.partial_error and on_partial_result is not None:
        on_partial_result(paged.partial_error)

    findings = []
    for raw_assessment in paged.items:
        props = raw_assessment.get("properties") or {}
        status_obj = props.get("status") or {}
        if (status_obj.get("code") or "").strip().lower() != "unhealthy":
            continue
        findings.append(normalize_assessment(raw_assessment, now=now))
    return findings
