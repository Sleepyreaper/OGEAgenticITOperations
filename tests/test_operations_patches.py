#!/usr/bin/env python3
"""Test Azure Update Manager patch compliance normalization
(app/operations/collectors/patches.py) -- missing critical/security
update detection, stale/failed assessment detection, case-insensitive
classification-key parsing, and explicit Resource Graph failure
surfacing.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_patches.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import patches  # noqa: E402
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

ROWS = [
    {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1/patchAssessmentResults/latest",
     "name": "guid1", "properties": {
         "osType": "Windows", "lastModifiedDateTime": (NOW - timedelta(hours=2)).isoformat(),
         "availablePatchCountByClassification": {"critical": 2, "security": 1}, "errorDetails": [],
     }},
    {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm2/patchAssessmentResults/latest",
     "name": "guid2", "properties": {
         "osType": "Linux", "lastModifiedDateTime": (NOW - timedelta(days=10)).isoformat(),
         "availablePatchCountByClassification": {}, "errorDetails": [],
     }},
    {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm3/patchAssessmentResults/latest",
     "name": "guid3", "properties": {
         "osType": "Windows", "lastModifiedDateTime": (NOW - timedelta(hours=1)).isoformat(),
         "availablePatchCountByClassification": {}, "errorDetails": ["agent unreachable"],
     }},
    {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm4/patchAssessmentResults/latest",
     "name": "guid4", "properties": {
         # Uppercase classification keys -- must still be parsed correctly (case-insensitive).
         "osType": "Windows", "lastModifiedDateTime": (NOW - timedelta(hours=1)).isoformat(),
         "availablePatchCountByClassification": {"Critical": 0, "Security": 3}, "errorDetails": [],
     }},
]


# ─── Normalization + severity ───────────────────────────────────────────
print("\n\U0001f9ea Test 1: collect_patch_compliance -- missing updates, stale/failed assessments, case-insensitive keys")
findings = patches.collect_patch_compliance(["sub1"], stale_days=7, query_fn=lambda q, subscription_ids: ROWS, now=NOW)
by_resource = {}
for f in findings:
    by_resource.setdefault(f.resource_id, []).append(f)

test("vm1 gets a missing-updates Finding (2 critical, 1 security)", any("Missing critical/security" in f.title for f in by_resource.get("/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1", [])))
vm1_finding = next(f for f in findings if "vm1" in f.title)
test("critical updates present -> high severity", vm1_finding.severity == "high")
test("vm1's Finding uses category patch", vm1_finding.category == "patch")

vm2_findings = by_resource.get("/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm2", [])
test("vm2 (10 days stale, no errors) gets a stale-assessment Finding, not a missing-updates one", len(vm2_findings) == 1 and "stale" in vm2_findings[0].title.lower())
test("a stale (not errored) assessment is medium severity", vm2_findings[0].severity == "medium")

vm3_findings = by_resource.get("/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm3", [])
test("vm3 (errorDetails present) gets a failed-assessment Finding", len(vm3_findings) == 1 and "failed" in vm3_findings[0].title.lower())
test("an errored assessment is high severity", vm3_findings[0].severity == "high")

vm4_findings = by_resource.get("/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm4", [])
test("vm4's uppercase 'Critical'/'Security' classification keys are still parsed (case-insensitive)", len(vm4_findings) == 1 and vm4_findings[0].metadata["security_count"] == 3)

test("the underlying VM resource id is derived by stripping /patchAssessmentResults/latest", vm1_finding.resource_id == "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1")

# ─── Explicit failure surfacing ─────────────────────────────────────────
print("\n\U0001f9ea Test 2: a Resource Graph query failure raises OperationsCollectionError, never an empty success")


def failing_query(q, subscription_ids):
    raise RuntimeError("throttled")


try:
    patches.collect_patch_compliance(["sub1"], query_fn=failing_query)
    test("a failing ARG query raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("a failing ARG query raises OperationsCollectionError instead of returning []", True)

try:
    patches.collect_patch_compliance([], query_fn=lambda q, subscription_ids: ROWS)
    test("empty subscription_ids raises ValueError", False)
except ValueError:
    test("empty subscription_ids raises ValueError", True)

try:
    patches.collect_patch_compliance(["sub1"], stale_days=0, query_fn=lambda q, subscription_ids: ROWS)
    test("a non-positive stale_days raises ValueError", False)
except ValueError:
    test("a non-positive stale_days raises ValueError", True)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
