"""Fired/recently-resolved Azure Monitor alerts, normalized to Findings.

Uses the subscription-scoped Microsoft.AlertsManagement "Alerts - List"
REST API (no azure-mgmt-* SDK covers this surface) via
app.operations.collectors.http.paginated_get, so this stays DI-friendly,
makes no real network calls in tests, and follows the API's `nextLink`
(bounded -- see paginated_get) instead of silently returning only the
first page.
"""

from datetime import timedelta
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
    utc_now,
)

__all__ = ["normalize_alert", "collect_fired_alerts"]

SOURCE = EvidenceSource.AZURE_MONITOR_ALERT.value
API_VERSION = "2019-05-05-preview"

_SEVERITY_MAP = {
    "sev0": Severity.CRITICAL,
    "sev1": Severity.HIGH,
    "sev2": Severity.MEDIUM,
    "sev3": Severity.LOW,
    "sev4": Severity.INFORMATIONAL,
}

# The Alerts API's `timeRange` query param only accepts these coarse
# buckets. We request the smallest bucket that covers the requested
# lookback, then filter precisely by timestamp below -- the API's coarse
# bucketing is never the final word on what's "in scope".
_TIME_RANGE_BUCKETS = [(1, "1h"), (24, "1d"), (24 * 7, "7d"), (24 * 30, "30d")]


def _time_range_for_lookback(lookback_hours: int) -> str:
    for hours, bucket in _TIME_RANGE_BUCKETS:
        if lookback_hours <= hours:
            return bucket
    return _TIME_RANGE_BUCKETS[-1][1]


def _severity_from_raw(raw: Optional[str]) -> Severity:
    key = (raw or "").strip().lower()
    if key not in _SEVERITY_MAP:
        raise OperationsCollectionError(SOURCE, f"unrecognized alert severity {raw!r}")
    return _SEVERITY_MAP[key]


def normalize_alert(raw_alert: dict, *, resource_owner_lookup: Optional[Callable[[str], str]] = None) -> Finding:
    """Normalize one Microsoft.AlertsManagement alert payload into a Finding."""
    props = raw_alert.get("properties") or {}
    essentials = props.get("essentials") or {}

    alert_id = raw_alert.get("id") or raw_alert.get("name") or ""
    if not alert_id:
        raise OperationsCollectionError(SOURCE, "alert payload is missing an id")

    severity = _severity_from_raw(essentials.get("severity"))
    monitor_condition = (essentials.get("monitorCondition") or "").strip().lower()
    alert_state = (essentials.get("alertState") or "").strip().lower()

    if monitor_condition == "resolved":
        status = FindingStatus.RESOLVED
    elif alert_state == "acknowledged":
        status = FindingStatus.ACKNOWLEDGED
    else:
        status = FindingStatus.OPEN

    start_raw = essentials.get("startDateTime")
    if not start_raw:
        raise OperationsCollectionError(SOURCE, f"alert {alert_id} is missing startDateTime")
    last_modified_raw = essentials.get("lastModifiedDateTime") or start_raw
    first_seen = ensure_utc_iso(start_raw, field_name=f"alert {alert_id}.startDateTime")
    last_seen = ensure_utc_iso(last_modified_raw, field_name=f"alert {alert_id}.lastModifiedDateTime")
    if parse_utc_iso(last_seen) < parse_utc_iso(first_seen):
        last_seen = first_seen  # tolerate minor clock skew between the two fields rather than fail the alert

    resource_id = essentials.get("targetResource") or None
    owner = ""
    if resource_owner_lookup and resource_id:
        owner = resource_owner_lookup(resource_id) or ""

    target_name = essentials.get("targetResourceName") or resource_id or "an unknown resource"
    title = essentials.get("alertRule") or raw_alert.get("name") or "Azure Monitor alert"
    summary = essentials.get("description") or (
        f"{essentials.get('monitorService', 'Azure Monitor')} alert on {target_name}"
    )
    resolved = status == FindingStatus.RESOLVED

    evidence = [EvidenceReference(
        source=SOURCE,
        title=title,
        observed_at=first_seen,
        resource_id=resource_id,
        reference=alert_id,
        raw_excerpt=summary,
    )]

    return Finding(
        category=FindingCategory.INCIDENT.value,
        severity=severity.value,
        status=status.value,
        title=title,
        summary=summary,
        business_impact=(
            f"Alert resolved after firing at {severity.value} severity on {target_name}."
            if resolved else
            f"Active {severity.value}-severity alert on {target_name}."
        ),
        first_seen=first_seen,
        last_seen=last_seen,
        source=SOURCE,
        owner=owner,
        resource_id=resource_id,
        affected_resource_count=1 if resource_id else 0,
        confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=evidence,
        recommended_action="" if resolved else "Investigate the firing alert condition on the target resource.",
        approval_required=False,
        executive_attention=(not resolved) and severity in (Severity.CRITICAL, Severity.HIGH),
        metadata={
            "alert_id": alert_id,
            "monitor_condition": monitor_condition,
            "alert_state": alert_state,
            "signal_type": essentials.get("signalType", ""),
            "target_resource_type": essentials.get("targetResourceType", ""),
        },
        discriminator=alert_id,
    )


def collect_fired_alerts(
    subscription_id: str,
    *,
    lookback_hours: int = 24,
    resource_owner_lookup: Optional[Callable[[str], str]] = None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    now=None,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> list:
    """Fired (monitor condition == Fired) and recently-resolved alerts for
    one subscription, normalized to Findings.

    `now`/`lookback_hours` bound the result to alerts last modified
    within the lookback window -- raises ValueError for a non-positive
    lookback (no silent clamping).

    `max_pages`/`max_records` bound how many `nextLink` pages/alerts this
    call will ever follow/accumulate (see
    app.operations.collectors.http.paginated_get) -- a bounded, non-fatal
    stop that never blocks other sources' collection."""
    if not subscription_id:
        raise ValueError("subscription_id is required")
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")

    now_dt = now or utc_now()
    cutoff = now_dt - timedelta(hours=lookback_hours)

    paged = paginated_get(
        f"/subscriptions/{subscription_id}/providers/Microsoft.AlertsManagement/alerts",
        source=SOURCE,
        params={"api-version": API_VERSION, "timeRange": _time_range_for_lookback(lookback_hours)},
        credential_factory=credential_factory,
        http_get=http_get,
        max_pages=max_pages,
        max_records=max_records,
    )

    findings = []
    for raw_alert in paged.items:
        finding = normalize_alert(raw_alert, resource_owner_lookup=resource_owner_lookup)
        if parse_utc_iso(finding.last_seen) < cutoff:
            continue
        findings.append(finding)
    return findings
