#!/usr/bin/env python3
"""Test the bounded, cached operations snapshot service
(app/operations/snapshot.py) -- combining Phase 1/2 + legacy-scan
envelopes, deduplication, caching (incl. force_refresh), workflow-state
merge, and truthful status semantics (never "ok" when every applicable
source failed).

All collection functions are injected fakes; no real Azure calls, and no
module-level singleton cache/state store is touched (everything is
passed in explicitly).

Run: python3 tests/test_operations_snapshot.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.cache import SnapshotCache  # noqa: E402
from app.operations.config import OperationsConfig  # noqa: E402
from app.operations.models import (  # noqa: E402
    ConfidenceLevel, EvidenceReference, EvidenceSource, Finding, FindingCategory, FindingStatus, Severity,
)
from app.operations.service import CollectionEnvelope  # noqa: E402
from app.operations.snapshot import get_snapshot  # noqa: E402
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


DB_PATH = str(REPO_ROOT / "tests" / "_test_operations_snapshot.db")


def _cleanup():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup()
NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)
CONFIG = OperationsConfig()


def make_finding(disc, *, category=FindingCategory.SECURITY.value, severity=Severity.HIGH.value, exec_att=True, approval=False, source=EvidenceSource.RESOURCE_GRAPH.value):
    return Finding(
        category=category, severity=severity, status=FindingStatus.OPEN.value,
        title=f"Finding {disc}", summary="s", business_impact="b",
        first_seen="2025-06-01T00:00:00Z", last_seen="2025-06-01T00:00:00Z",
        source=source, confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=[EvidenceReference(source=source, title="t", observed_at="2025-06-01T00:00:00Z")],
        executive_attention=exec_att, approval_required=approval, discriminator=disc,
    )


def new_store():
    _cleanup()
    return OperationsStateStore(DB_PATH)


print("\n\U0001f9ea Test 1: get_snapshot -- combines full + legacy envelopes, deduplicates by id")
f1 = make_finding("dup-1")
f1_again = make_finding("dup-1")  # same category/source/discriminator -> same deterministic id
f2 = make_finding("f2", category=FindingCategory.COST.value, severity=Severity.LOW.value, exec_att=False)


def fake_full_collect(subs, *, config, **kwargs):
    return [CollectionEnvelope(source="azure_monitor_alerts", status="ok", collected_at="2025-06-01T00:00:00Z", findings=[f1])]


def fake_legacy_collect(subs, **kwargs):
    return [
        {"source": "legacy_security_drift", "status": "ok", "collected_at": "2025-06-01T00:00:00Z", "findings": [f1_again], "summaries": [], "error": None},
        {"source": "legacy_resource_hygiene", "status": "ok", "collected_at": "2025-06-01T00:00:00Z", "findings": [f2], "summaries": [], "error": None},
    ]


store1 = new_store()
cache1 = SnapshotCache(ttl_seconds=60)
snap = get_snapshot(["SubA"], config=CONFIG, cache=cache1, state_store=store1, full_collect_fn=fake_full_collect, legacy_collect_fn=fake_legacy_collect, now=NOW)
test("snapshot has an id", snap.id.startswith("snap-"))
test("status is ok when everything succeeds", snap.status == "ok")
test("duplicate finding (same id) is merged -- 2 unique findings, not 3", len(snap.findings) == 2)
test("subscription_ids is normalized", snap.subscription_ids == ("suba",))
test("summary.total_findings matches", snap.summary["total_findings"] == 2)
test("findings are wrapped with finding/workflow/priority keys", set(snap.findings[0].keys()) == {"finding", "workflow", "priority"})
test("to_dict() round-trips cleanly", isinstance(snap.to_dict(), dict) and snap.to_dict()["status"] == "ok")


print("\n\U0001f9ea Test 2: get_snapshot -- caching (hit avoids re-collection, force_refresh bypasses it)")
calls = {"full": 0, "legacy": 0}


def counting_full(subs, *, config, **kwargs):
    calls["full"] += 1
    return [CollectionEnvelope(source="azure_monitor_alerts", status="ok", collected_at="2025-06-01T00:00:00Z", findings=[])]


def counting_legacy(subs, **kwargs):
    calls["legacy"] += 1
    return []


store2 = new_store()
cache2 = SnapshotCache(ttl_seconds=60)
get_snapshot(["SubB"], config=CONFIG, cache=cache2, state_store=store2, full_collect_fn=counting_full, legacy_collect_fn=counting_legacy, now=NOW)
test("first call collects once", calls["full"] == 1 and calls["legacy"] == 1)
get_snapshot(["subb"], config=CONFIG, cache=cache2, state_store=store2, full_collect_fn=counting_full, legacy_collect_fn=counting_legacy, now=NOW)
test("second call (different case, same normalized key) is a cache hit -- no new collection", calls["full"] == 1 and calls["legacy"] == 1)
get_snapshot(["SubB"], config=CONFIG, cache=cache2, state_store=store2, full_collect_fn=counting_full, legacy_collect_fn=counting_legacy, force_refresh=True, now=NOW)
test("force_refresh=True bypasses the cache and re-collects", calls["full"] == 2 and calls["legacy"] == 2)


print("\n\U0001f9ea Test 3: get_snapshot -- never caches a failure as a successful empty result")
store3 = new_store()
cache3 = SnapshotCache(ttl_seconds=60)


def failing_full(subs, *, config, **kwargs):
    return [CollectionEnvelope(source="azure_monitor_alerts", status="error", collected_at="2025-06-01T00:00:00Z", error="ARM auth failed")]


def empty_legacy(subs, **kwargs):
    return []


snap_fail = get_snapshot(["SubC"], config=CONFIG, cache=cache3, state_store=store3, full_collect_fn=failing_full, legacy_collect_fn=empty_legacy, now=NOW)
test("all-applicable-sources-failed snapshot status is 'error', not 'ok'", snap_fail.status == "error")
test("source_errors records the failing source", len(snap_fail.source_errors) == 1 and snap_fail.source_errors[0]["source"] == "azure_monitor_alerts")
snap_fail_cached = get_snapshot(["SubC"], config=CONFIG, cache=cache3, state_store=store3, full_collect_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be cache hit")), legacy_collect_fn=empty_legacy, now=NOW)
test("the error status IS what gets cached (truthfully) -- cache hit still reports error", snap_fail_cached.status == "error")


print("\n\U0001f9ea Test 4: get_snapshot -- partial failure (some sources ok, some error) -> status='partial'")
store4 = new_store()
cache4 = SnapshotCache(ttl_seconds=60)


def partial_full(subs, *, config, **kwargs):
    return [
        CollectionEnvelope(source="azure_monitor_alerts", status="ok", collected_at="2025-06-01T00:00:00Z", findings=[]),
        CollectionEnvelope(source="capacity", status="error", collected_at="2025-06-01T00:00:00Z", error="quota API down"),
    ]


snap_partial = get_snapshot(["SubD"], config=CONFIG, cache=cache4, state_store=store4, full_collect_fn=partial_full, legacy_collect_fn=empty_legacy, now=NOW)
test("mixed ok/error -> status is 'partial'", snap_partial.status == "partial")


print("\n\U0001f9ea Test 5: get_snapshot -- not_configured sources alone don't count as failures")
store5 = new_store()
cache5 = SnapshotCache(ttl_seconds=60)


def all_not_configured(subs, *, config, **kwargs):
    return [CollectionEnvelope(source="workload_slo", status="not_configured", collected_at="2025-06-01T00:00:00Z", error="no SLOs defined")]


snap_nc = get_snapshot(["SubE"], config=CONFIG, cache=cache5, state_store=store5, full_collect_fn=all_not_configured, legacy_collect_fn=empty_legacy, now=NOW)
test("all-not_configured (nothing applicable failed) -> status 'ok'", snap_nc.status == "ok")


print("\n\U0001f9ea Test 6: get_snapshot -- workflow state is merged onto findings")
store6 = new_store()
cache6 = SnapshotCache(ttl_seconds=60)
f_wf = make_finding("workflow-test")


def wf_full(subs, *, config, **kwargs):
    return [CollectionEnvelope(source="azure_monitor_alerts", status="ok", collected_at="2025-06-01T00:00:00Z", findings=[f_wf])]


store6.apply_action(f_wf.id, "acknowledge", actor="alice", now=NOW)
snap_wf = get_snapshot(["SubF"], config=CONFIG, cache=cache6, state_store=store6, full_collect_fn=wf_full, legacy_collect_fn=empty_legacy, now=NOW)
test("the pre-acknowledged finding shows workflow status 'acknowledged'", snap_wf.findings[0]["workflow"]["status"] == "acknowledged")


print("\n\U0001f9ea Test 7: get_snapshot -- rejects an empty subscription_ids list")
try:
    get_snapshot([], config=CONFIG, cache=SnapshotCache(ttl_seconds=60), state_store=new_store())
    test("empty subscription_ids raises ValueError", False)
except ValueError:
    test("empty subscription_ids raises ValueError", True)


print("\n\U0001f9ea Test 8: get_snapshot -- priority ordering places the higher-severity finding first")
store8 = new_store()
cache8 = SnapshotCache(ttl_seconds=60)
low = make_finding("low-1", severity=Severity.LOW.value, exec_att=False)
critical = make_finding("critical-1", severity=Severity.CRITICAL.value, exec_att=True)


def ordering_full(subs, *, config, **kwargs):
    return [CollectionEnvelope(source="azure_monitor_alerts", status="ok", collected_at="2025-06-01T00:00:00Z", findings=[low, critical])]


snap_order = get_snapshot(["SubG"], config=CONFIG, cache=cache8, state_store=store8, full_collect_fn=ordering_full, legacy_collect_fn=empty_legacy, now=NOW)
test("critical severity finding ranks before low severity", snap_order.findings[0]["finding"]["id"] == critical.id)
test("priority band is exposed (P1 for critical)", snap_order.findings[0]["priority"]["band"] == "P1")


_cleanup()

# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
