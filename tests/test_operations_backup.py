#!/usr/bin/env python3
"""Test Azure Backup job/protected-item health collectors
(app/operations/collectors/backup.py) -- failed/stuck-in-progress job
detection, protection-error/stale protected-item detection, and explicit
Log Analytics query failure surfacing.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_backup.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import backup  # noqa: E402
from app.operations.errors import OperationsCollectionError  # noqa: E402

PASS = 0
FAIL = 0


def test(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \u2705 {name}")
    else:
        FAIL += 1
        print(f"  \u274c {name}")


NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)

JOB_ROWS = [
    {"TimeGenerated": NOW, "JobUniqueId": "j1", "JobStatus": "Failed", "JobOperation": "Backup",
     "JobStartDateTime": NOW - timedelta(hours=1), "JobFailureCode": "UserErrorX",
     "BackupItemFriendlyName": "vm1", "BackupItemUniqueId": "bi1", "DatasourceResourceId": "/subscriptions/s/rg/vm1",
     "VaultName": "vault1", "ResourceGroupName": "rg"},
    {"TimeGenerated": NOW, "JobUniqueId": "j2", "JobStatus": "InProgress", "JobOperation": "Backup",
     "JobStartDateTime": NOW - timedelta(hours=30), "BackupItemFriendlyName": "vm2", "BackupItemUniqueId": "bi2",
     "DatasourceResourceId": "/subscriptions/s/rg/vm2", "VaultName": "vault1", "ResourceGroupName": "rg"},
    {"TimeGenerated": NOW, "JobUniqueId": "j3", "JobStatus": "InProgress", "JobOperation": "Backup",
     "JobStartDateTime": NOW - timedelta(hours=1), "BackupItemFriendlyName": "vm3", "BackupItemUniqueId": "bi3",
     "DatasourceResourceId": "/subscriptions/s/rg/vm3", "VaultName": "vault1", "ResourceGroupName": "rg"},
]

ITEM_ROWS = [
    {"TimeGenerated": NOW, "BackupItemUniqueId": "bi1", "BackupItemFriendlyName": "vm1", "BackupItemProtectionState": "ProtectionError",
     "DatasourceResourceId": "/subscriptions/s/rg/vm1", "LatestRecoveryPointTime": NOW - timedelta(days=1), "ProtectedContainerFriendlyName": "vault1"},
    {"TimeGenerated": NOW, "BackupItemUniqueId": "bi2", "BackupItemFriendlyName": "vm2", "BackupItemProtectionState": "Protected",
     "DatasourceResourceId": "/subscriptions/s/rg/vm2", "LatestRecoveryPointTime": NOW - timedelta(days=10), "ProtectedContainerFriendlyName": "vault1"},
    {"TimeGenerated": NOW, "BackupItemUniqueId": "bi3", "BackupItemFriendlyName": "vm3", "BackupItemProtectionState": "Protected",
     "DatasourceResourceId": "/subscriptions/s/rg/vm3", "LatestRecoveryPointTime": NOW - timedelta(hours=1), "ProtectedContainerFriendlyName": "vault1"},
    {"TimeGenerated": NOW, "BackupItemUniqueId": "bi4", "BackupItemFriendlyName": "vm4", "BackupItemProtectionState": "ProtectionStopped",
     "DatasourceResourceId": "/subscriptions/s/rg/vm4", "LatestRecoveryPointTime": None, "ProtectedContainerFriendlyName": "vault1"},
]


# ─── Backup jobs -- Failed + stuck InProgress become Findings ─────────
print("\n\U0001f9ea Test 1: get_backup_jobs / backup_job_findings -- failed + stuck-in-progress detection")
jobs = backup.get_backup_jobs(query_logs_fn=lambda q, w, t: JOB_ROWS)
test("all 3 rows are captured as raw jobs", len(jobs) == 3)
job_findings = backup.backup_job_findings(jobs, now=NOW)
test("exactly 2 Findings: 1 failed job, 1 stuck-in-progress job (fresh in-progress is not flagged)", len(job_findings) == 2)
by_title = {f.title: f for f in job_findings}
failed_finding = next(f for f in job_findings if "j1" in "".join(e.reference or "" for e in f.evidence))
test("the failed job Finding is high severity", failed_finding.severity == "high")
test("the failed job Finding demands executive attention", failed_finding.executive_attention is True)
stuck_finding = next(f for f in job_findings if f is not failed_finding)
test("the stuck in-progress job Finding is medium severity", stuck_finding.severity == "medium")
test("all backup job Findings use category backup", all(f.category == "backup" for f in job_findings))
test("resource_id captures the protected item's DatasourceResourceId", failed_finding.resource_id == "/subscriptions/s/rg/vm1")

# ─── Protected item health -- protection errors + stale items ─────────
print("\n\U0001f9ea Test 2: get_protected_item_health / protected_item_findings -- protection errors + stale items")
items = backup.get_protected_item_health(query_logs_fn=lambda q, w, t: ITEM_ROWS)
test("all 4 rows are captured as raw items", len(items) == 4)
item_findings = backup.protected_item_findings(items, stale_days=3, now=NOW)
test("exactly 2 Findings: 1 protection error, 1 stale (ProtectionStopped is excluded, fresh recovery point is healthy)", len(item_findings) == 2)
error_finding = next(f for f in item_findings if "protection error" in f.title.lower())
stale_finding = next(f for f in item_findings if "stale" in f.title.lower())
test("protection error Finding is high severity", error_finding.severity == "high")
test("stale-backup Finding is medium severity", stale_finding.severity == "medium")
test("ProtectionStopped items never produce a Finding (deliberately not protected)", "vm4" not in [f.metadata.get("resource_id") for f in item_findings] and all("vm4" not in (f.resource_id or "") for f in item_findings))
test("all protected-item Findings use category backup", all(f.category == "backup" for f in item_findings))

# ─── Explicit failure surfacing ─────────────────────────────────────────
print("\n\U0001f9ea Test 3: a Log Analytics query failure raises OperationsCollectionError, never an empty success")


def failing_query(q, w, t):
    raise RuntimeError("workspace unreachable")


try:
    backup.get_backup_jobs(query_logs_fn=failing_query)
    test("a failing jobs query raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("a failing jobs query raises OperationsCollectionError instead of returning []", True)

try:
    backup.get_protected_item_health(query_logs_fn=failing_query)
    test("a failing protected-item query raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("a failing protected-item query raises OperationsCollectionError instead of returning []", True)

try:
    backup.get_backup_jobs(lookback_hours=0)
    test("a non-positive lookback_hours raises ValueError", False)
except ValueError:
    test("a non-positive lookback_hours raises ValueError", True)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
