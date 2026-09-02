#!/usr/bin/env python3
"""Test the persistent finding workflow-state store
(app/operations/state.py) -- SQLite transitions, strict validation,
audit history, snooze expiry, and the merge_workflow_state helper.

Uses a real (but disposable, repo-local, deleted at start/end) SQLite
file -- never /tmp.

Run: python3 tests/test_operations_state.py
"""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.models import (  # noqa: E402
    ConfidenceLevel, EvidenceReference, EvidenceSource, Finding, FindingCategory, FindingStatus, Severity,
)
from app.operations.state import (  # noqa: E402
    DEFAULT_WORKFLOW_STATUS, OperationsStateError, OperationsStateStore, WORKFLOW_ACTIONS, WORKFLOW_STATUSES,
    merge_workflow_state,
)

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


DB_PATH = str(REPO_ROOT / "tests" / "_test_operations_state.db")


def _cleanup():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup()
NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)

store = OperationsStateStore(DB_PATH)

print("\n\U0001f9ea Test 1: FindingStateRecord -- status validation")
try:
    from app.operations.state import FindingStateRecord
    FindingStateRecord(finding_id="f1", status="bogus")
    test("an unrecognized status raises OperationsStateError", False)
except OperationsStateError:
    test("an unrecognized status raises OperationsStateError", True)


print("\n\U0001f9ea Test 2: get_state -- synthetic default for an untouched finding")
default_state = store.get_state("never-touched", now=NOW)
test("default status is 'new'", default_state.status == DEFAULT_WORKFLOW_STATUS)
test("default assigned_owner is empty", default_state.assigned_owner == "")


print("\n\U0001f9ea Test 3: apply_action -- acknowledge -> start -> resolve chain")
rec = store.apply_action("fnd-1", "acknowledge", actor="alice", first_seen="2025-05-30T00:00:00Z", now=NOW)
test("acknowledge: new -> acknowledged", rec.status == "acknowledged")
test("first_seen_at is preserved from the caller", rec.first_seen_at == "2025-05-30T00:00:00.000Z")

rec = store.apply_action("fnd-1", "start", actor="bob", now=NOW)
test("start: acknowledged -> in_progress", rec.status == "in_progress")

rec = store.apply_action("fnd-1", "resolve", actor="bob", reason="root cause fixed", now=NOW)
test("resolve: in_progress -> resolved", rec.status == "resolved")
test("disposition_reason is recorded", rec.disposition_reason == "root cause fixed")


print("\n\U0001f9ea Test 4: apply_action -- strict transition validation (no silent no-ops)")
try:
    store.apply_action("fnd-1", "acknowledge", actor="alice", now=NOW)
    test("re-acknowledging a resolved finding raises OperationsStateError", False)
except OperationsStateError:
    test("re-acknowledging a resolved finding raises OperationsStateError", True)

try:
    store.apply_action("fnd-2", "start", actor="alice", now=NOW)
    test("'start' from an implicit 'new' with no prior row is allowed", True)
except OperationsStateError:
    test("'start' from an implicit 'new' with no prior row is allowed", False)


print("\n\U0001f9ea Test 5: apply_action -- unknown action / missing actor / missing finding_id")
try:
    store.apply_action("fnd-3", "bogus_action", actor="alice", now=NOW)
    test("an unknown action raises OperationsStateError", False)
except OperationsStateError:
    test("an unknown action raises OperationsStateError", True)

try:
    store.apply_action("fnd-3", "acknowledge", actor="", now=NOW)
    test("a blank actor raises OperationsStateError", False)
except OperationsStateError:
    test("a blank actor raises OperationsStateError", True)

try:
    store.apply_action("", "acknowledge", actor="alice", now=NOW)
    test("a blank finding_id raises OperationsStateError", False)
except OperationsStateError:
    test("a blank finding_id raises OperationsStateError", True)


print("\n\U0001f9ea Test 6: apply_action -- snooze validation and restricted allowed_from")
try:
    store.apply_action("fnd-4", "snooze", actor="alice", now=NOW)
    test("snooze with no snooze_until raises OperationsStateError", False)
except OperationsStateError:
    test("snooze with no snooze_until raises OperationsStateError", True)

try:
    store.apply_action("fnd-4", "snooze", actor="alice", snooze_until="2025-01-01T00:00:00Z", now=NOW)
    test("snooze with a past snooze_until raises OperationsStateError", False)
except OperationsStateError:
    test("snooze with a past snooze_until raises OperationsStateError", True)

store.apply_action("fnd-4", "start", actor="alice", now=NOW)
try:
    store.apply_action("fnd-4", "snooze", actor="alice", snooze_until="2025-06-02T00:00:00Z", now=NOW)
    test("snooze from 'in_progress' is disallowed (only new/acknowledged)", False)
except OperationsStateError:
    test("snooze from 'in_progress' is disallowed (only new/acknowledged)", True)


print("\n\U0001f9ea Test 7: apply_action -- assign requires an owner, works from multiple statuses")
try:
    store.apply_action("fnd-5", "assign", actor="alice", now=NOW)
    test("assign with no owner raises OperationsStateError", False)
except OperationsStateError:
    test("assign with no owner raises OperationsStateError", True)

rec = store.apply_action("fnd-5", "assign", actor="alice", owner="team-x", now=NOW)
test("assign from an implicit 'new' keeps status 'new'", rec.status == "new")
test("assign sets assigned_owner", rec.assigned_owner == "team-x")

store.apply_action("fnd-5", "acknowledge", actor="alice", now=NOW)
rec = store.apply_action("fnd-5", "assign", actor="alice", owner="team-y", now=NOW)
test("assign from 'acknowledged' preserves status", rec.status == "acknowledged")
test("assign updates the owner", rec.assigned_owner == "team-y")


print("\n\U0001f9ea Test 8: snooze expiry -- auto-reverts to the pre-snooze status")
store.apply_action("fnd-6", "snooze", actor="carol", snooze_until="2025-06-01T01:00:00Z", first_seen="2025-06-01T00:00:00Z", now=NOW)
rec_before = store.get_state("fnd-6", now=NOW + timedelta(minutes=30))
test("still snoozed before expiry", rec_before.status == "snoozed")
rec_after = store.get_state("fnd-6", now=NOW + timedelta(hours=2))
test("auto-reverts to 'new' after expiry (was 'new' before snoozing)", rec_after.status == "new")
test("snooze_until is cleared after expiry", rec_after.snooze_until is None)

store.apply_action("fnd-7", "acknowledge", actor="carol", now=NOW)
store.apply_action("fnd-7", "snooze", actor="carol", snooze_until="2025-06-01T01:00:00Z", now=NOW)
rec_after2 = store.get_state("fnd-7", now=NOW + timedelta(hours=2))
test("auto-reverts to 'acknowledged' after expiry (was 'acknowledged' before snoozing)", rec_after2.status == "acknowledged")

history = store.get_audit_history("fnd-6")
test("an 'auto_unsnooze' audit row exists after get_state resolves the expiry", any(h["action"] == "auto_unsnooze" for h in history))


print("\n\U0001f9ea Test 9: get_audit_history -- most-recent-first, dismiss/resolve reasons recorded")
history = store.get_audit_history("fnd-1")
test("history is newest-first", history[0]["action"] == "resolve" and history[-1]["action"] == "acknowledge")
test("resolve's from_status/to_status are recorded", history[0]["from_status"] == "in_progress" and history[0]["to_status"] == "resolved")

try:
    store.get_audit_history("fnd-1", limit=0)
    test("limit=0 raises ValueError", False)
except ValueError:
    test("limit=0 raises ValueError", True)


print("\n\U0001f9ea Test 10: get_states -- batch lookup, missing ids omitted")
batch = store.get_states(["fnd-1", "fnd-5", "does-not-exist"], now=NOW)
test("returns entries only for ids with a persisted row", set(batch.keys()) == {"fnd-1", "fnd-5"})
test("fnd-1's batched status matches get_state", batch["fnd-1"].status == store.get_state("fnd-1", now=NOW).status)


print("\n\U0001f9ea Test 11: dismiss")
store.apply_action("fnd-8", "dismiss", actor="dave", reason="false positive", now=NOW)
rec = store.get_state("fnd-8", now=NOW)
test("dismiss from implicit 'new' works", rec.status == "dismissed")
test("disposition_reason recorded for dismiss", rec.disposition_reason == "false positive")


print("\n\U0001f9ea Test 12: record_handoff / get_latest_handoff / list_handoffs")
test("no handoff exists yet", store.get_latest_handoff() is None)
h1 = store.record_handoff(created_by="alice", content_hash="hash1", open_finding_ids=["fnd-1", "fnd-2"], summary={"open": 2}, now=NOW)
test("record_handoff returns an id", isinstance(h1["id"], int))
h2 = store.record_handoff(created_by="bob", content_hash="hash2", open_finding_ids=["fnd-3"], summary={"open": 1}, now=NOW + timedelta(hours=8))
latest = store.get_latest_handoff()
test("get_latest_handoff returns the most recent one", latest["content_hash"] == "hash2")
listed = store.list_handoffs(limit=10)
test("list_handoffs returns both, newest first", len(listed) == 2 and listed[0]["content_hash"] == "hash2")

try:
    store.list_handoffs(limit=0)
    test("list_handoffs(limit=0) raises ValueError", False)
except ValueError:
    test("list_handoffs(limit=0) raises ValueError", True)


print("\n\U0001f9ea Test 13: merge_workflow_state -- default for untouched, real state for touched")


def make_finding(disc):
    return Finding(
        category=FindingCategory.SECURITY.value, severity=Severity.HIGH.value, status=FindingStatus.OPEN.value,
        title=f"Finding {disc}", summary="s", business_impact="b",
        first_seen="2025-06-01T00:00:00Z", last_seen="2025-06-01T00:00:00Z",
        source=EvidenceSource.RESOURCE_GRAPH.value, confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=[EvidenceReference(source=EvidenceSource.RESOURCE_GRAPH.value, title="t", observed_at="2025-06-01T00:00:00Z")],
        discriminator=disc,
    )


untouched = make_finding("untouched-1")
touched = make_finding("fnd-1-alias")
store.apply_action(touched.id, "acknowledge", actor="alice", now=NOW)

merged = merge_workflow_state([untouched, touched], store, now=NOW)
merged_by_id = {m["finding"]["id"]: m for m in merged}
test("untouched finding gets the default workflow state", merged_by_id[untouched.id]["workflow"]["status"] == DEFAULT_WORKFLOW_STATUS)
test("touched finding reflects its real persisted state", merged_by_id[touched.id]["workflow"]["status"] == "acknowledged")


print("\n\U0001f9ea Test 14: concurrency -- concurrent apply_action calls on distinct findings don't crash/corrupt")
errors = []


def worker(n):
    try:
        fid = f"concurrent-{n}"
        store.apply_action(fid, "acknowledge", actor=f"worker-{n}", now=NOW)
        store.apply_action(fid, "start", actor=f"worker-{n}", now=NOW)
        store.apply_action(fid, "resolve", actor=f"worker-{n}", now=NOW)
    except Exception as exc:  # pragma: no cover
        errors.append(exc)


threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
for t in threads:
    t.start()
for t in threads:
    t.join()
test("no exceptions across 10 concurrent workers on distinct findings", errors == [])
test("every worker's finding ends resolved", all(store.get_state(f"concurrent-{n}", now=NOW).status == "resolved" for n in range(10)))


print("\n\U0001f9ea Test 15: module constants are consistent")
test("WORKFLOW_STATUSES has exactly the 6 documented values", set(WORKFLOW_STATUSES) == {"new", "acknowledged", "in_progress", "resolved", "dismissed", "snoozed"})
test("WORKFLOW_ACTIONS has exactly the 6 documented actions", set(WORKFLOW_ACTIONS) == {"acknowledge", "start", "resolve", "dismiss", "snooze", "assign"})


_cleanup()

# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
