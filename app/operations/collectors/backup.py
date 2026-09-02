"""Azure Backup: failed/in-progress-stuck backup jobs and protected-item
health, normalized to Findings.

Both signals come from the Log Analytics tables Azure Backup's
diagnostic settings populate (AddonAzureBackupJobs, CoreAzureBackup) via
the existing Log Analytics helper (app.azure_data.query_logs), matching
changes.py/slo.py's DI convention. This is a deliberate assumption --
see docs/AZURE_DATA_SOURCES.md -- rather than enumerating Recovery
Services vaults and calling the per-vault Backup Jobs/Items ARM REST API
for each one:

  - It generically covers every Recovery Services vault sending
    diagnostics to the configured workspace in ONE query, with no vault
    enumeration/pagination required (bounded, cheap).
  - It requires the vault(s) to have diagnostic settings configured
    sending AzureBackupJobs/AzureBackupProtectedInstance categories to a
    Log Analytics workspace -- if that isn't set up, both collectors
    return an empty (not missing) result. This is the same class of
    assumption workload_slo/changes already make about Log Analytics
    availability, not a new one specific to backup.
"""

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
    utc_now,
)

__all__ = [
    "get_backup_jobs",
    "backup_job_findings",
    "get_protected_item_health",
    "protected_item_findings",
]

JOB_SOURCE = EvidenceSource.BACKUP_JOB.value
ITEM_SOURCE = EvidenceSource.BACKUP_PROTECTED_ITEM.value

QueryLogsFn = Callable[..., list]

# A job still 'InProgress' after this long is treated as stuck/stale --
# an explicit, documented assumption (most Azure Backup jobs for VM/SQL/
# file-share workloads complete well within 24h); see
# docs/AZURE_DATA_SOURCES.md.
STALE_IN_PROGRESS_HOURS = 24

_JOB_STATUSES_OF_INTEREST = {"failed", "completedwithwarnings", "inprogress"}
# Protected items in this state are deliberately not being backed up
# (soft-deleted/decommissioned) -- not a hygiene gap.
_EXCLUDED_PROTECTION_STATES = {"protectionstopped"}


def get_backup_jobs(
    *,
    lookback_hours: int = 24,
    workspace_id: Optional[str] = None,
    query_logs_fn: QueryLogsFn = default_query_logs,
) -> list:
    """Raw (not yet Finding-ized) Failed/CompletedWithWarnings/InProgress
    backup jobs from AddonAzureBackupJobs, newest first."""
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    query = (
        "AddonAzureBackupJobs "
        f"| where TimeGenerated > ago({lookback_hours}h) "
        "| where JobStatus in ('Failed', 'CompletedWithWarnings', 'InProgress') "
        "| project TimeGenerated, JobUniqueId, JobStatus, JobOperation, JobStartDateTime, JobFailureCode, "
        "BackupItemFriendlyName, BackupItemUniqueId, DatasourceResourceId, VaultName, ResourceGroupName "
        "| order by TimeGenerated desc | take 500"
    )
    try:
        rows = query_logs_fn(query, workspace_id, timedelta(hours=lookback_hours))
    except Exception as exc:
        raise OperationsCollectionError(JOB_SOURCE, "Log Analytics backup jobs query failed", detail=str(exc)) from exc

    jobs = []
    for row in rows:
        timestamp = row.get("TimeGenerated")
        if timestamp is None:
            continue
        start_time_raw = row.get("JobStartDateTime")
        jobs.append({
            "time_generated": ensure_utc_iso(timestamp, field_name="AddonAzureBackupJobs.TimeGenerated"),
            "job_id": row.get("JobUniqueId") or "",
            "status": row.get("JobStatus") or "",
            "operation": row.get("JobOperation") or "",
            "start_time": ensure_utc_iso(start_time_raw, field_name="AddonAzureBackupJobs.JobStartDateTime") if start_time_raw else None,
            "failure_code": row.get("JobFailureCode") or "",
            "item_name": row.get("BackupItemFriendlyName") or "",
            "item_id": row.get("BackupItemUniqueId") or "",
            "resource_id": row.get("DatasourceResourceId") or "",
            "vault_name": row.get("VaultName") or "",
            "resource_group": row.get("ResourceGroupName") or "",
        })
    return jobs


def backup_job_findings(jobs: list, *, stale_in_progress_hours: int = STALE_IN_PROGRESS_HOURS, now=None) -> list:
    """Failed/CompletedWithWarnings jobs, and 'InProgress' jobs stuck
    beyond `stale_in_progress_hours`, become Findings. Successful/still-
    fresh in-progress jobs are not (mirrors changes.py's "only failures
    become Findings" convention)."""
    now = now or utc_now()
    findings = []
    for job in jobs:
        status_lower = job.get("status", "").strip().lower()
        resource_id = job.get("resource_id") or None
        target = job.get("item_name") or resource_id or "an unknown backup item"

        if status_lower in ("failed", "completedwithwarnings"):
            severity = Severity.HIGH if status_lower == "failed" else Severity.MEDIUM
            failure_detail = f" (failure code {job['failure_code']})" if job.get("failure_code") else ""
            findings.append(Finding(
                category=FindingCategory.BACKUP.value,
                severity=severity.value,
                status=FindingStatus.OPEN.value,
                title=f"Backup job {job.get('status')}: {target}",
                summary=f"{job.get('operation') or 'Backup'} job {job.get('status', '').lower()} for {target}{failure_detail}.",
                business_impact="A backup job did not complete cleanly -- the most recent recovery point for this item may be missing or incomplete.",
                first_seen=job["time_generated"],
                last_seen=job["time_generated"],
                source=JOB_SOURCE,
                resource_id=resource_id,
                affected_resource_count=1 if resource_id else 0,
                confidence=ConfidenceLevel.CONFIRMED.value,
                evidence=[EvidenceReference(
                    source=JOB_SOURCE,
                    title=f"Backup job: {job.get('operation') or 'Backup'}",
                    observed_at=job["time_generated"],
                    resource_id=resource_id,
                    reference=job.get("job_id") or None,
                    raw_excerpt=f"status={job.get('status', '')}; failureCode={job.get('failure_code', '')}; vault={job.get('vault_name', '')}",
                )],
                recommended_action="Review the job failure details in the Recovery Services vault and retry the backup.",
                approval_required=False,
                executive_attention=status_lower == "failed",
                metadata={"job_status": job.get("status", ""), "vault_name": job.get("vault_name", ""), "failure_code": job.get("failure_code", "")},
                discriminator=job.get("job_id") or f"{job['time_generated']}|{target}",
            ))
        elif status_lower == "inprogress" and job.get("start_time"):
            age_hours = (now - parse_utc_iso(job["start_time"])).total_seconds() / 3600.0
            if age_hours >= stale_in_progress_hours:
                findings.append(Finding(
                    category=FindingCategory.BACKUP.value,
                    severity=Severity.MEDIUM.value,
                    status=FindingStatus.OPEN.value,
                    title=f"Backup job stuck in-progress: {target}",
                    summary=f"{job.get('operation') or 'Backup'} job for {target} has been InProgress for {age_hours:.1f}h (>= {stale_in_progress_hours}h).",
                    business_impact="A stuck backup job may be holding a lock on the protected item or masking a silent failure.",
                    first_seen=job["start_time"],
                    last_seen=job["time_generated"],
                    source=JOB_SOURCE,
                    resource_id=resource_id,
                    affected_resource_count=1 if resource_id else 0,
                    confidence=ConfidenceLevel.DERIVED.value,
                    evidence=[EvidenceReference(
                        source=JOB_SOURCE,
                        title=f"Backup job: {job.get('operation') or 'Backup'}",
                        observed_at=job["time_generated"],
                        resource_id=resource_id,
                        reference=job.get("job_id") or None,
                        raw_excerpt=f"status=InProgress; startTime={job['start_time']}; vault={job.get('vault_name', '')}",
                    )],
                    recommended_action="Check the Recovery Services vault for a stuck job and cancel/retry if it is no longer making progress.",
                    approval_required=False,
                    executive_attention=False,
                    metadata={"vault_name": job.get("vault_name", ""), "age_hours": round(age_hours, 1)},
                    discriminator=job.get("job_id") or f"{job['start_time']}|{target}",
                ))
    return findings


def get_protected_item_health(
    *,
    workspace_id: Optional[str] = None,
    query_logs_fn: QueryLogsFn = default_query_logs,
) -> list:
    """The latest known health snapshot (protection state, most recent
    recovery point) for every protected item reporting into
    CoreAzureBackup, one row per BackupItemUniqueId."""
    query = (
        "CoreAzureBackup "
        "| summarize arg_max(TimeGenerated, *) by BackupItemUniqueId "
        "| project TimeGenerated, BackupItemUniqueId, BackupItemFriendlyName, BackupItemProtectionState, "
        "DatasourceResourceId, LatestRecoveryPointTime, ProtectedContainerFriendlyName, ResourceGroupName "
        "| take 1000"
    )
    try:
        rows = query_logs_fn(query, workspace_id, timedelta(days=1))
    except Exception as exc:
        raise OperationsCollectionError(ITEM_SOURCE, "Log Analytics protected-item health query failed", detail=str(exc)) from exc

    items = []
    for row in rows:
        timestamp = row.get("TimeGenerated")
        if timestamp is None:
            continue
        recovery_point_raw = row.get("LatestRecoveryPointTime")
        items.append({
            "time_generated": ensure_utc_iso(timestamp, field_name="CoreAzureBackup.TimeGenerated"),
            "item_id": row.get("BackupItemUniqueId") or "",
            "item_name": row.get("BackupItemFriendlyName") or "",
            "protection_state": row.get("BackupItemProtectionState") or "",
            "resource_id": row.get("DatasourceResourceId") or "",
            "latest_recovery_point": ensure_utc_iso(recovery_point_raw, field_name="CoreAzureBackup.LatestRecoveryPointTime") if recovery_point_raw else None,
            "container_name": row.get("ProtectedContainerFriendlyName") or "",
            "resource_group": row.get("ResourceGroupName") or "",
        })
    return items


def protected_item_findings(items: list, *, stale_days: int = 3, now=None) -> list:
    """Protection-error items, and items with no recovery point within
    `stale_days`, become Findings. Deliberately-stopped protection
    (ProtectionStopped) is excluded -- not a hygiene gap."""
    now = now or utc_now()
    findings = []
    for item in items:
        state_lower = (item.get("protection_state") or "").strip().lower()
        if state_lower in _EXCLUDED_PROTECTION_STATES:
            continue
        resource_id = item.get("resource_id") or None
        target = item.get("item_name") or resource_id or "an unknown protected item"

        if "error" in state_lower:
            findings.append(Finding(
                category=FindingCategory.BACKUP.value,
                severity=Severity.HIGH.value,
                status=FindingStatus.OPEN.value,
                title=f"Backup protection error: {target}",
                summary=f"{target} is reporting protection state '{item.get('protection_state')}'.",
                business_impact="This item's backup protection is in an error state -- it may not be recoverable if needed.",
                first_seen=item["time_generated"],
                last_seen=item["time_generated"],
                source=ITEM_SOURCE,
                resource_id=resource_id,
                affected_resource_count=1 if resource_id else 0,
                confidence=ConfidenceLevel.CONFIRMED.value,
                evidence=[EvidenceReference(
                    source=ITEM_SOURCE,
                    title=f"Protected item: {target}",
                    observed_at=item["time_generated"],
                    resource_id=resource_id,
                    reference=item.get("item_id") or None,
                    raw_excerpt=f"protectionState={item.get('protection_state', '')}; container={item.get('container_name', '')}",
                )],
                recommended_action="Investigate the protection error in the Recovery Services vault and re-protect the item if needed.",
                approval_required=False,
                executive_attention=True,
                metadata={"protection_state": item.get("protection_state", ""), "container_name": item.get("container_name", "")},
                discriminator=item.get("item_id") or f"{item['time_generated']}|{target}",
            ))
            continue

        recovery_point = item.get("latest_recovery_point")
        age_days = None if recovery_point is None else (now - parse_utc_iso(recovery_point)).total_seconds() / 86400.0
        if recovery_point is None or age_days >= stale_days:
            age_text = "no recovery point on record" if recovery_point is None else f"last recovery point {age_days:.1f}d old"
            findings.append(Finding(
                category=FindingCategory.BACKUP.value,
                severity=Severity.MEDIUM.value,
                status=FindingStatus.OPEN.value,
                title=f"Stale backup: {target}",
                summary=f"{target} has {age_text} (threshold: {stale_days}d).",
                business_impact="A stale protected item means a restore would recover data older than the acceptable recovery point objective.",
                first_seen=item["time_generated"],
                last_seen=item["time_generated"],
                source=ITEM_SOURCE,
                resource_id=resource_id,
                affected_resource_count=1 if resource_id else 0,
                confidence=ConfidenceLevel.DERIVED.value,
                evidence=[EvidenceReference(
                    source=ITEM_SOURCE,
                    title=f"Protected item: {target}",
                    observed_at=item["time_generated"],
                    resource_id=resource_id,
                    reference=item.get("item_id") or None,
                    raw_excerpt=f"latestRecoveryPointTime={recovery_point or 'none'}; container={item.get('container_name', '')}",
                )],
                recommended_action="Confirm the backup schedule for this item is running and investigate why no recent recovery point exists.",
                approval_required=False,
                executive_attention=False,
                metadata={"protection_state": item.get("protection_state", ""), "stale_days_threshold": stale_days},
                discriminator=item.get("item_id") or f"{item['time_generated']}|{target}",
            ))
    return findings
