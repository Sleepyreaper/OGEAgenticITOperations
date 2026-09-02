#!/usr/bin/env python3
"""Test Service Health retirement/deprecation advisory normalization
(app/operations/collectors/advisories.py) -- deadline-driven severity,
the no-deadline informational case, and explicit Resource Graph failure
surfacing.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_advisories.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import advisories  # noqa: E402
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
    {"id": "/subscriptions/s/providers/Microsoft.ResourceHealth/events/e1", "name": "e1", "subscriptionId": "s",
     "eventSubType": "Retirement", "title": "Retiring API version", "summaryText": "Old API retiring soon.",
     "trackingId": "TRK1", "priority": "1", "impactStartTime": (NOW - timedelta(days=30)).isoformat(),
     "impactMitigationTime": (NOW + timedelta(days=60)).isoformat()},
    {"id": "/subscriptions/s/providers/Microsoft.ResourceHealth/events/e2", "name": "e2", "subscriptionId": "s",
     "eventSubType": "Retirement", "title": "Distant retirement", "summaryText": "Retiring eventually.",
     "trackingId": "TRK2", "priority": "3", "impactStartTime": (NOW - timedelta(days=5)).isoformat(),
     "impactMitigationTime": (NOW + timedelta(days=300)).isoformat()},
    {"id": "/subscriptions/s/providers/Microsoft.ResourceHealth/events/e3", "name": "e3", "subscriptionId": "s",
     "eventSubType": "InformationalAdvisory", "title": "FYI advisory", "summaryText": "No deadline set.",
     "trackingId": "TRK3", "priority": "4", "impactStartTime": None, "impactMitigationTime": None},
]


def query_fn(query, subscription_ids):
    return ROWS


# ─── Successful normalization + deadline-driven severity ───────────────
print("\n\U0001f9ea Test 1: collect_retirement_advisories -- deadline-driven severity, no-deadline informational case")
findings = advisories.collect_retirement_advisories(["sub1"], warning_days=180, query_fn=query_fn, now=NOW)
test("all 3 active advisories are normalized (not filtered to eventSubType == Retirement only)", len(findings) == 3)
by_tracking = {f.metadata["tracking_id"]: f for f in findings}

near_deadline = by_tracking["TRK1"]
test("a deadline within the warning window (60d <= 180d) is high severity", near_deadline.severity == "high")
test("a near-deadline advisory demands executive attention", near_deadline.executive_attention is True)
test("the deadline is surfaced in metadata (never dropped)", near_deadline.metadata["deadline"] is not None)
test("days_to_deadline is computed and exposed, not hidden", near_deadline.metadata["days_to_deadline"] == 60.0)

far_deadline = by_tracking["TRK2"]
test("a deadline beyond the warning window (300d > 180d) is medium severity, not high", far_deadline.severity == "medium")

no_deadline = by_tracking["TRK3"]
test("an advisory with no published deadline is low severity (never an invented deadline)", no_deadline.severity == "low")
test("an advisory with no published deadline has metadata.deadline == None", no_deadline.metadata["deadline"] is None)
test("all retirement/advisory Findings use category compliance", all(f.category == "compliance" for f in findings))
test("all retirement/advisory Findings use source service_health", all(f.source == "service_health" for f in findings))

# ─── Explicit failure surfacing ─────────────────────────────────────────
print("\n\U0001f9ea Test 2: a Resource Graph query failure raises OperationsCollectionError, never an empty success")


def failing_query(query, subscription_ids):
    raise RuntimeError("throttled")


try:
    advisories.collect_retirement_advisories(["sub1"], query_fn=failing_query)
    test("a failing ARG query raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("a failing ARG query raises OperationsCollectionError instead of returning []", True)

try:
    advisories.collect_retirement_advisories([], query_fn=query_fn)
    test("empty subscription_ids raises ValueError", False)
except ValueError:
    test("empty subscription_ids raises ValueError", True)

try:
    advisories.collect_retirement_advisories(["sub1"], warning_days=0, query_fn=query_fn)
    test("a non-positive warning_days raises ValueError", False)
except ValueError:
    test("a non-positive warning_days raises ValueError", True)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
