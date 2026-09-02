#!/usr/bin/env python3
"""Test the structured shift-handoff service (app/operations/handoff.py)
-- open/new/changed/snoozed buckets, capacity watch, recent changes,
source gaps, the content hash, and that persistence never stores raw
evidence/secrets.

Run: python3 tests/test_operations_handoff.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.handoff import build_handoff, persist_handoff  # noqa: E402
from app.operations.models import (  # noqa: E402
    CapacitySummary, ConfidenceLevel, EvidenceReference, EvidenceSource, Finding, FindingCategory, FindingStatus,
    Severity,
)
from app.operations.service import CollectionEnvelope  # noqa: E402
from app.operations.snapshot import OperationsSnapshot  # noqa: E402
from app.operations.state import OperationsStateStore  # noqa: E402

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


DB_PATH = str(REPO_ROOT / "tests" / "_test_operations_handoff.db")


def _cleanup():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup()
NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)


def make_finding(disc, *, category=FindingCategory.SECURITY.value, severity=Severity.HIGH.value, first_seen="2025-06-01T00:00:00Z", approval=False):
    return Finding(
        category=category, severity=severity, status=FindingStatus.OPEN.value,
        title=f"Finding {disc}", summary="s", business_impact="b",
        first_seen=first_seen, last_seen=first_seen,
        source=EvidenceSource.RESOURCE_GRAPH.value, confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=[EvidenceReference(source=EvidenceSource.RESOURCE_GRAPH.value, title="t", observed_at=first_seen)],
        approval_required=approval, discriminator=disc,
    )


def wrap(finding, *, workflow_status="new", snooze_until=None, reason="", updated_at=None):
    return {
        "finding": finding.to_dict(),
        "workflow": {"status": workflow_status, "assigned_owner": "", "disposition_reason": reason, "snooze_until": snooze_until, "first_seen_at": None, "created_at": None, "updated_at": updated_at},
        "priority": {"band": "P2", "factors": {"customer_impact": False, "severity_rank": 1, "slo_state": None, "slo_state_rank": 2, "age_hours": 1.0, "confidence_rank": 0}},
    }


def make_snapshot(findings_with_workflow, envelopes, coverage=None):
    return OperationsSnapshot(
        id="snap-test", generated_at="2025-06-01T00:00:00.000Z", subscription_ids=("sub1",), status="ok",
        envelopes=envelopes, findings=findings_with_workflow,
        coverage=coverage or {"total_sources": len(envelopes), "sources_by_status": {"ok": [e.source for e in envelopes], "error": [], "not_configured": [], "not_supported": []}},
        source_errors=[], summary={},
    )


print("\n\U0001f9ea Test 1: build_handoff -- open/snoozed/pending_approvals buckets")
resolved = make_finding("resolved-1")
open_item = make_finding("open-1", approval=True)
snoozed_item = make_finding("snoozed-1")
snap = make_snapshot([
    wrap(resolved, workflow_status="resolved"),
    wrap(open_item, workflow_status="new"),
    wrap(snoozed_item, workflow_status="snoozed", snooze_until="2025-06-02T00:00:00Z", reason="waiting on vendor"),
], [])
handoff = build_handoff(snap, now=NOW)
test("resolved items are excluded from open_items", resolved.id not in {i["id"] for i in handoff["open_items"]})
test("open_item_count matches (resolved and snoozed both excluded)", handoff["open_item_count"] == 1)
test("snoozed item appears in snoozed_items with its snooze_until/reason", handoff["snoozed_items"][0]["id"] == snoozed_item.id and handoff["snoozed_items"][0]["snooze_until"] == "2025-06-02T00:00:00Z" and handoff["snoozed_items"][0]["disposition_reason"] == "waiting on vendor")
test("pending_approvals includes the approval-required open item", any(i["id"] == open_item.id for i in handoff["pending_approvals"]))
test("content_hash is present and looks like a hex digest", isinstance(handoff["content_hash"], str) and len(handoff["content_hash"]) == 32)


print("\n\U0001f9ea Test 2: build_handoff -- no prior handoff -> everything open counts as new")
test("with no state_store, everything open is 'new'", {i["id"] for i in handoff["new_since_prior"]} == {open_item.id})
test("changed_since_prior is empty with no prior handoff", handoff["changed_since_prior"] == [])
test("prior_handoff_at is None", handoff["prior_handoff_at"] is None)


print("\n\U0001f9ea Test 3: build_handoff -- new/changed since a persisted prior handoff")
store = OperationsStateStore(DB_PATH)
prior_time = NOW - timedelta(hours=8)
store.record_handoff(created_by="alice", content_hash="prior-hash", open_finding_ids=[], summary={}, now=prior_time)

old_item = make_finding("old-1", first_seen="2025-05-01T00:00:00Z")  # first_seen predates the prior handoff
new_item = make_finding("new-1", first_seen="2025-06-01T23:00:00Z")  # first_seen is after NOW? use a time between prior and NOW
# new_item's first_seen must be AFTER prior_time and can be before/around NOW
new_item_finding = make_finding("new-2", first_seen=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
changed_item = wrap(old_item, workflow_status="acknowledged", updated_at=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))

snap3 = make_snapshot([
    wrap(old_item, workflow_status="new"),  # unchanged since prior (updated_at None)
    wrap(new_item_finding, workflow_status="new"),  # new since prior (first_seen after prior_time)
], [])
handoff3 = build_handoff(snap3, state_store=store, now=NOW)
test("prior_handoff_at reflects the persisted prior handoff", handoff3["prior_handoff_at"] == prior_time.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
test("the old item (first_seen before prior handoff, unchanged) is not 'new'", old_item.id not in {i["id"] for i in handoff3["new_since_prior"]})
test("the new item (first_seen after prior handoff) IS 'new'", new_item_finding.id in {i["id"] for i in handoff3["new_since_prior"]})

snap3b = make_snapshot([changed_item], [])
handoff3b = build_handoff(snap3b, state_store=store, now=NOW)
test("an item with workflow.updated_at after the prior handoff (but first_seen before it) is 'changed', not 'new'", old_item.id in {i["id"] for i in handoff3b["changed_since_prior"]} and old_item.id not in {i["id"] for i in handoff3b["new_since_prior"]})


print("\n\U0001f9ea Test 4: build_handoff -- capacity_watch, recent_changes, source_gaps")
cap_warning = CapacitySummary(resource_scope="compute:eastus", metric="cores", current=80, limit=100, threshold_state="warning", evaluated_at="2025-06-01T00:00:00.000Z", headroom_pct=20.0)
cap_healthy = CapacitySummary(resource_scope="compute:westus", metric="cores", current=10, limit=100, threshold_state="healthy", evaluated_at="2025-06-01T00:00:00.000Z", headroom_pct=90.0)
change_finding = Finding(
    category=FindingCategory.CHANGE.value, severity=Severity.MEDIUM.value, status=FindingStatus.OPEN.value,
    title="Failed deployment", summary="s", business_impact="b",
    first_seen="2025-05-31T23:00:00Z", last_seen="2025-05-31T23:30:00Z",
    source=EvidenceSource.ACTIVITY_LOG.value, confidence=ConfidenceLevel.CONFIRMED.value,
    evidence=[EvidenceReference(source=EvidenceSource.ACTIVITY_LOG.value, title="t", observed_at="2025-05-31T23:30:00Z")],
    discriminator="change-1",
)
envelopes = [
    CollectionEnvelope(source="capacity", status="ok", collected_at="2025-06-01T00:00:00.000Z", summaries=[cap_warning, cap_healthy]),
    CollectionEnvelope(source="activity_log_change_health", status="ok", collected_at="2025-06-01T00:00:00.000Z", findings=[change_finding]),
    CollectionEnvelope(source="cost_management_trend", status="error", collected_at="2025-06-01T00:00:00.000Z", error="boom"),
    CollectionEnvelope(source="workload_slo", status="not_configured", collected_at="2025-06-01T00:00:00.000Z", error="no SLOs"),
]
snap4 = make_snapshot([], envelopes, coverage={
    "total_sources": 4,
    "sources_by_status": {"ok": ["capacity", "activity_log_change_health"], "error": ["cost_management_trend"], "not_configured": ["workload_slo"], "not_supported": []},
})
handoff4 = build_handoff(snap4, now=NOW)
test("capacity_watch includes only the warning summary, not the healthy one", len(handoff4["capacity_watch"]) == 1 and handoff4["capacity_watch"][0]["resource_scope"] == "compute:eastus")
test("recent_changes includes the change finding within 24h", any(c["id"] == change_finding.id for c in handoff4["recent_changes"]))
test("source_gaps includes the errored and not_configured sources", {g["source"] for g in handoff4["source_gaps"]} == {"cost_management_trend", "workload_slo"})


print("\n\U0001f9ea Test 5: persist_handoff -- never stores raw evidence text, only ids/counts/hash")
persisted = persist_handoff(handoff, state_store=store, created_by="alice", now=NOW)
test("persisted record has an id", isinstance(persisted["id"], int))
test("persisted content_hash matches the handoff's own hash", persisted["content_hash"] == handoff["content_hash"])
latest = store.get_latest_handoff()
test("the persisted open_finding_ids are id strings only (no titles/business_impact/evidence)", all(isinstance(i, str) for i in latest["open_finding_ids"]))
test("the persisted summary is counts only", set(latest["summary"].keys()) == {"open_item_count", "new_since_prior_count", "changed_since_prior_count", "snoozed_count", "pending_approvals_count", "source_gap_count"})


_cleanup()

# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
