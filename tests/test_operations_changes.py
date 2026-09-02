#!/usr/bin/env python3
"""Test the Activity Log change timeline and its deterministic
correlation with Resource Health degradation events
(app/operations/collectors/changes.py).

query_logs_fn is injected (matching app.azure_data.query_logs's
signature) -- no real Log Analytics/network call is made.

Run: python3 tests/test_operations_changes.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import changes  # noqa: E402
from app.operations.errors import OperationsCollectionError  # noqa: E402
from app.operations.models import ConfidenceLevel, FindingCategory, Severity  # noqa: E402

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


NOW = datetime.now(timezone.utc)
VM_ID = "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"


def rows_fn(rows):
    return lambda query, workspace_id, timespan: rows


def failing_rows_fn(exc):
    def _fn(query, workspace_id, timespan):
        raise exc
    return _fn


# ─── get_change_timeline ──────────────────────────────────────────────
print("\n\U0001f9ea Test 1: get_change_timeline -- normalizes AzureActivity rows")
raw_rows = [{
    "TimeGenerated": NOW, "OperationNameValue": "Microsoft.Compute/virtualMachines/write",
    "ActivityStatusValue": "Succeeded", "ResourceGroup": "rg1", "ResourceId": VM_ID,
    "Caller": "alice@example.com", "CorrelationId": "corr-1",
}]
changes_list = changes.get_change_timeline(lookback_hours=24, query_logs_fn=rows_fn(raw_rows))
test("returns one normalized change", len(changes_list) == 1)
test("timestamp is canonical UTC ISO", changes_list[0]["timestamp"].endswith("Z"))
test("resource_id is preserved", changes_list[0]["resource_id"] == VM_ID)
test("status is preserved", changes_list[0]["status"] == "Succeeded")

try:
    changes.get_change_timeline(lookback_hours=0)
    test("a non-positive lookback_hours raises ValueError", False)
except ValueError:
    test("a non-positive lookback_hours raises ValueError", True)

try:
    changes.get_change_timeline(lookback_hours=24, query_logs_fn=failing_rows_fn(RuntimeError("workspace unreachable")))
    test("a Log Analytics query failure raises OperationsCollectionError (not an empty list)", False)
except OperationsCollectionError:
    test("a Log Analytics query failure raises OperationsCollectionError (not an empty list)", True)


# ─── get_resource_health_events ───────────────────────────────────────
print("\n\U0001f9ea Test 2: get_resource_health_events -- parses Properties_d JSON")
health_rows = [{
    "TimeGenerated": NOW, "OperationNameValue": "Microsoft.ResourceHealth/healthevent/action",
    "ResourceGroup": "rg1", "ResourceId": VM_ID,
    "Properties_d": '{"currentHealthStatus": "Unavailable", "previousHealthStatus": "Available", "cause": "PlatformInitiated", "title": "VM unavailable"}',
}]
events = changes.get_resource_health_events(lookback_hours=24, query_logs_fn=rows_fn(health_rows))
test("returns one normalized health event", len(events) == 1)
test("current_status is parsed from Properties_d JSON", events[0]["current_status"] == "Unavailable")
test("cause is parsed from Properties_d JSON", events[0]["cause"] == "PlatformInitiated")

malformed_rows = [{**health_rows[0], "Properties_d": "not json"}]
malformed_events = changes.get_resource_health_events(lookback_hours=24, query_logs_fn=rows_fn(malformed_rows))
test("malformed Properties_d degrades to an empty status rather than raising", malformed_events[0]["current_status"] == "")


# ─── correlate_changes_with_health ────────────────────────────────────
print("\n\U0001f9ea Test 3: correlate_changes_with_health -- same-resource match within the window")
change_time = NOW - timedelta(minutes=10)
one_change = [{
    "timestamp": change_time.isoformat(), "operation": "Microsoft.Compute/virtualMachines/write",
    "status": "Succeeded", "resource_group": "rg1", "resource_id": VM_ID,
    "caller": "alice@example.com", "correlation_id": "corr-1",
}]
unavailable_event = [{
    "timestamp": NOW.isoformat(), "operation": "Microsoft.ResourceHealth/healthevent/action",
    "resource_group": "rg1", "resource_id": VM_ID, "current_status": "Unavailable",
    "previous_status": "Available", "cause": "PlatformInitiated", "title": "VM unavailable",
}]

correlated = changes.correlate_changes_with_health(one_change, unavailable_event, correlation_window_minutes=60)
test("a change within the window produces one correlation Finding", len(correlated) == 1)
test("category is change", correlated[0].category == FindingCategory.CHANGE.value)
test("Unavailable health status maps to high severity", correlated[0].severity == Severity.HIGH.value)
test("confidence is correlated (deterministic correlation, not a guess)", correlated[0].confidence == ConfidenceLevel.CORRELATED.value)
test("approval_required is True (a rollback is a real action)", correlated[0].approval_required is True)
test("evidence includes both the health event and the matching change", len(correlated[0].evidence) == 2)

degraded_event = [{**unavailable_event[0], "current_status": "Degraded"}]
degraded_correlated = changes.correlate_changes_with_health(one_change, degraded_event, correlation_window_minutes=60)
test("Degraded health status maps to medium severity", degraded_correlated[0].severity == Severity.MEDIUM.value)

print("\n\U0001f9ea Test 4: correlate_changes_with_health -- no match outside the window or on a different resource")
far_change = [{**one_change[0], "timestamp": (NOW - timedelta(minutes=120)).isoformat()}]
test("a change outside the correlation window produces no Finding", changes.correlate_changes_with_health(far_change, unavailable_event, correlation_window_minutes=60) == [])

other_resource_change = [{**one_change[0], "resource_id": "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm2"}]
test("a change on a different resource produces no Finding", changes.correlate_changes_with_health(other_resource_change, unavailable_event, correlation_window_minutes=60) == [])

healthy_event = [{**unavailable_event[0], "current_status": "Available"}]
test("a healthy (non-degraded) status is never correlated", changes.correlate_changes_with_health(one_change, healthy_event, correlation_window_minutes=60) == [])

print("\n\U0001f9ea Test 5: correlate_changes_with_health -- resource-group fallback when the health event has no resource id")
event_no_resource_id = [{**unavailable_event[0], "resource_id": ""}]
rg_fallback = changes.correlate_changes_with_health(one_change, event_no_resource_id, correlation_window_minutes=60)
test("falls back to a resource-group match when the health event has no resource id", len(rg_fallback) == 1)

try:
    changes.correlate_changes_with_health([], [], correlation_window_minutes=0)
    test("a non-positive correlation_window_minutes raises ValueError", False)
except ValueError:
    test("a non-positive correlation_window_minutes raises ValueError", True)


# ─── get_failed_change_findings ───────────────────────────────────────
print("\n\U0001f9ea Test 6: get_failed_change_findings -- one Finding per failed change, none for successes")
mixed_changes = [
    {"timestamp": NOW.isoformat(), "operation": "Microsoft.Storage/storageAccounts/write", "status": "Failed",
     "resource_group": "rg1", "resource_id": VM_ID, "caller": "bob@example.com", "correlation_id": "corr-2"},
    {"timestamp": NOW.isoformat(), "operation": "Microsoft.Compute/virtualMachines/write", "status": "Succeeded",
     "resource_group": "rg1", "resource_id": VM_ID, "caller": "bob@example.com", "correlation_id": "corr-3"},
]
failed_findings = changes.get_failed_change_findings(mixed_changes)
test("exactly one Finding for the failed change", len(failed_findings) == 1)
test("successful changes do not become their own Finding", all("Failed change" in f.title for f in failed_findings))
test("confidence is confirmed (Activity Log states the outcome directly)", failed_findings[0].confidence == ConfidenceLevel.CONFIRMED.value)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
