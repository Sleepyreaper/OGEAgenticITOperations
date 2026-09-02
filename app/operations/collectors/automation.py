"""Azure Automation: failed/suspended jobs within a configurable
lookback, normalized to Findings.

Uses the Microsoft.Automation Jobs "List by Automation Account" REST API
(ARM REST via app.operations.collectors.http.paginated_get) for each caller-
supplied Automation Account resource ID -- callers discover these ids
(e.g. via Resource Graph) the same way capacity.py's `locations` and
keyvault.py's `vault_uris` are discovered/supplied by the caller, not by
this collector.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.operations.collectors.http import (
    CredentialFactory,
    DEFAULT_MAX_RECORDS,
    HttpGet,
    default_credential_factory,
    default_http_get,
    paginated_get,
)
from app.operations.models import (
    ConfidenceLevel,
    EvidenceReference,
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    Severity,
    ensure_utc_iso,
)

__all__ = ["normalize_job", "collect_automation_failures"]

SOURCE = EvidenceSource.AUTOMATION_JOB.value
API_VERSION = "2024-10-23"
_MAX_PAGES = 5  # bounded pagination -- never an unbounded crawl of one account's job history (see app.operations.collectors.http.paginated_get)

_STATUSES_OF_INTEREST = {"failed", "suspended"}
_SEVERITY_BY_STATUS = {"failed": Severity.HIGH, "suspended": Severity.MEDIUM}


def normalize_job(raw_job: dict, *, automation_account_id: str) -> Finding:
    """Normalize one Microsoft.Automation job payload (already filtered
    to Failed/Suspended -- see collect_automation_failures) into a
    Finding."""
    props = raw_job.get("properties") or {}
    job_id = raw_job.get("id") or raw_job.get("name") or ""
    if not job_id:
        raise ValueError("Automation job payload is missing an id")

    status_lower = (props.get("status") or "").strip().lower()
    severity = _SEVERITY_BY_STATUS.get(status_lower, Severity.MEDIUM)
    runbook_name = (props.get("runbook") or {}).get("name") or "unknown runbook"
    exception = props.get("exception") or ""

    creation_raw = props.get("creationTime") or props.get("startTime")
    if not creation_raw:
        raise ValueError(f"Automation job {job_id} is missing creationTime")
    first_seen = ensure_utc_iso(creation_raw, field_name=f"job {job_id}.creationTime")
    end_raw = props.get("endTime") or props.get("lastModifiedTime") or creation_raw
    last_seen = ensure_utc_iso(end_raw, field_name=f"job {job_id}.endTime")

    account_name = automation_account_id.rstrip("/").split("/")[-1]
    summary = f"Runbook '{runbook_name}' job {props.get('status', '')} in Automation Account '{account_name}'."
    if exception:
        summary = f"{summary} {str(exception)[:300]}"

    return Finding(
        category=FindingCategory.AUTOMATION.value,
        severity=severity.value,
        status=FindingStatus.OPEN.value,
        title=f"Automation job {props.get('status', '')}: {runbook_name}",
        summary=summary,
        business_impact="An Automation runbook did not complete successfully -- any operation it performs (patching, cleanup, remediation, etc.) did not happen as scheduled.",
        first_seen=first_seen,
        last_seen=last_seen,
        source=SOURCE,
        resource_id=automation_account_id,
        affected_resource_count=1,
        confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=[EvidenceReference(
            source=SOURCE,
            title=f"Automation job: {runbook_name}",
            observed_at=first_seen,
            resource_id=automation_account_id,
            reference=job_id,
            raw_excerpt=f"status={props.get('status', '')}; exception={str(exception)[:200]}",
        )],
        recommended_action="Review the job's exception/output in the Automation Account and re-run or fix the runbook.",
        approval_required=False,
        executive_attention=status_lower == "failed",
        metadata={"runbook_name": runbook_name, "automation_account": account_name, "status": props.get("status", "")},
        discriminator=job_id,
    )


def collect_automation_failures(
    automation_account_ids: list,
    *,
    lookback_hours: int = 24,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    now: Optional[datetime] = None,
    max_pages: int = _MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> list:
    """Failed/Suspended Automation jobs created within `lookback_hours`,
    across every Automation Account in `automation_account_ids`.

    `max_pages`/`max_records` bound how many `nextLink` pages/jobs this
    call will ever follow/accumulate PER ACCOUNT (see
    app.operations.collectors.http.paginated_get)."""
    if not automation_account_ids:
        raise ValueError("automation_account_ids must be a non-empty list")
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    odata_filter = f"properties/creationTime ge datetime'{cutoff_str}'"

    findings = []
    for account_id in automation_account_ids:
        paged = paginated_get(
            f"/{account_id.lstrip('/')}/jobs",
            source=SOURCE, params={"api-version": API_VERSION, "$filter": odata_filter},
            credential_factory=credential_factory, http_get=http_get,
            max_pages=max_pages, max_records=max_records,
        )
        for raw_job in paged.items:
            props = raw_job.get("properties") or {}
            if (props.get("status") or "").strip().lower() not in _STATUSES_OF_INTEREST:
                continue
            findings.append(normalize_job(raw_job, automation_account_id=account_id))
    return findings
