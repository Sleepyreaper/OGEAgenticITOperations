#!/usr/bin/env python3
"""Test the /api/operations/* Flask routes (app/operations/routes.py) --
request parsing, strict JSON validation, status codes, and that existing
APIs (e.g. /api/health) are preserved unchanged.

app.operations.routes.get_snapshot is monkeypatched to a canned fake (no
real Azure calls; snapshot-building itself is covered by
tests/test_operations_snapshot.py) -- this file tests ONLY the route
layer: query/body parsing, validation, response shaping, status codes.
The workflow-state/handoff routes exercise a REAL (but disposable,
repo-local) SQLite store via OPERATIONS_STATE_DB, since that IS the
route-layer behavior worth covering end-to-end.

Run: python3 tests/test_operations_routes.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Telemetry must be provably disabled, and the workflow-state DB must
# point at a disposable, repo-local (never /tmp) test file -- both must
# be set before app.main/app.operations.snapshot import and cache
# anything from the environment.
os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-test-1"
DB_PATH = str(REPO_ROOT / "tests" / "_test_operations_routes.db")
os.environ["OPERATIONS_STATE_DB"] = DB_PATH


def _cleanup_db():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup_db()

from app.main import create_app  # noqa: E402
from app.operations import routes as routes_mod  # noqa: E402
from app.operations.models import (  # noqa: E402
    ConfidenceLevel, EvidenceReference, EvidenceSource, Finding, FindingCategory, FindingStatus, Severity,
)
from app.operations.snapshot import OperationsSnapshot  # noqa: E402

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


def make_finding(disc, *, severity=Severity.HIGH.value, exec_att=True, approval=False):
    return Finding(
        category=FindingCategory.SECURITY.value, severity=severity, status=FindingStatus.OPEN.value,
        title=f"Finding {disc}", summary="s", business_impact="b",
        first_seen="2025-06-01T00:00:00Z", last_seen="2025-06-01T00:00:00Z",
        source=EvidenceSource.RESOURCE_GRAPH.value, confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=[EvidenceReference(source=EvidenceSource.RESOURCE_GRAPH.value, title="t", observed_at="2025-06-01T00:00:00Z")],
        executive_attention=exec_att, approval_required=approval, discriminator=disc,
    )


FINDING_A = make_finding("route-a", approval=True)
FINDING_B = make_finding("route-b", severity=Severity.LOW.value, exec_att=False)

CANNED_SNAPSHOT = OperationsSnapshot(
    id="snap-canned", generated_at="2025-06-01T00:00:00.000Z", subscription_ids=("sub-test-1",), status="ok",
    envelopes=[],
    findings=[
        {"finding": FINDING_A.to_dict(), "workflow": {"status": "new", "assigned_owner": "", "disposition_reason": "", "snooze_until": None, "first_seen_at": None, "created_at": None, "updated_at": None}, "priority": {"band": "P1", "factors": {"customer_impact": True, "severity_rank": 0, "slo_state": None, "slo_state_rank": 2, "age_hours": 1.0, "confidence_rank": 0}}},
        {"finding": FINDING_B.to_dict(), "workflow": {"status": "new", "assigned_owner": "", "disposition_reason": "", "snooze_until": None, "first_seen_at": None, "created_at": None, "updated_at": None}, "priority": {"band": "P4", "factors": {"customer_impact": False, "severity_rank": 3, "slo_state": None, "slo_state_rank": 2, "age_hours": 1.0, "confidence_rank": 0}}},
    ],
    coverage={"total_sources": 0, "sources_by_status": {"ok": [], "error": [], "not_configured": [], "not_supported": []}},
    source_errors=[],
    summary={"total_findings": 2},
)


def fake_get_snapshot(sub_ids, **kwargs):
    return CANNED_SNAPSHOT


routes_mod.get_snapshot = fake_get_snapshot

app = create_app()
client = app.test_client()


print("\n\U0001f9ea Test 1: GET /api/operations/snapshot")
resp = client.get("/api/operations/snapshot")
test("200 OK", resp.status_code == 200)
data = resp.get_json()
test("returns the canned snapshot's id", data["id"] == "snap-canned")
test("findings count matches", len(data["findings"]) == 2)


print("\n\U0001f9ea Test 2: GET /api/operations/brief")
resp = client.get("/api/operations/brief")
test("200 OK", resp.status_code == 200)
data = resp.get_json()
test("overall_state is present", data["overall_state"] in ("healthy", "attention", "impact", "unknown"))
test("brief never includes a resource_id in evidence (no subscription id leak)", "resource_id" not in str(data.get("business_impact", {}).get("details", [])))


print("\n\U0001f9ea Test 3: GET /api/operations/queue -- filters and pagination")
resp = client.get("/api/operations/queue")
test("200 OK", resp.status_code == 200)
data = resp.get_json()
test("total is 2", data["total"] == 2)
test("top item is the P1 critical finding", data["items"][0]["id"] == FINDING_A.id)

resp = client.get("/api/operations/queue?severity=low")
test("severity filter -> 200 with 1 result", resp.status_code == 200 and resp.get_json()["total"] == 1)

resp = client.get("/api/operations/queue?category=not-a-real-category")
test("invalid category filter -> 400", resp.status_code == 400)

resp = client.get("/api/operations/queue?page=0")
test("invalid page -> 400", resp.status_code == 400)

resp = client.get("/api/operations/queue?page_size=99999")
test("out-of-range page_size -> 400", resp.status_code == 400)


print("\n\U0001f9ea Test 4: PATCH /api/operations/findings/<id> -- strict validation")
resp = client.patch(f"/api/operations/findings/{FINDING_A.id}", data="not json", content_type="text/plain")
test("non-JSON body -> 400", resp.status_code == 400)

resp = client.patch(f"/api/operations/findings/{FINDING_A.id}", json={})
test("missing action -> 400", resp.status_code == 400)

resp = client.patch(f"/api/operations/findings/{FINDING_A.id}", json={"action": "bogus", "actor": "alice"})
test("unrecognized action -> 400", resp.status_code == 400)

resp = client.patch(f"/api/operations/findings/{FINDING_A.id}", json={"action": "acknowledge"})
test("missing actor -> 400", resp.status_code == 400)

resp = client.patch(f"/api/operations/findings/{FINDING_A.id}", json={"action": "snooze", "actor": "alice"})
test("snooze missing snooze_until -> 400", resp.status_code == 400)

resp = client.patch(f"/api/operations/findings/{FINDING_A.id}", json={"action": "assign", "actor": "alice"})
test("assign missing owner -> 400", resp.status_code == 400)

resp = client.patch(f"/api/operations/findings/{FINDING_A.id}", json={"action": "acknowledge", "actor": "alice"})
test("valid acknowledge -> 200", resp.status_code == 200)
test("response reflects the new status", resp.get_json()["status"] == "acknowledged")

resp = client.patch(f"/api/operations/findings/{FINDING_A.id}", json={"action": "acknowledge", "actor": "alice"})
test("re-acknowledging an already-acknowledged finding -> 409 (illegal transition)", resp.status_code == 409)

resp = client.patch(f"/api/operations/findings/{FINDING_B.id}", json={"action": "assign", "actor": "bob", "owner": "team-x"})
test("assign with a valid owner -> 200", resp.status_code == 200)
test("assigned owner is reflected", resp.get_json()["assigned_owner"] == "team-x")


print("\n\U0001f9ea Test 5: GET /api/operations/evidence/<finding_id>")
resp = client.get(f"/api/operations/evidence/{FINDING_A.id}")
test("200 OK for a real finding id", resp.status_code == 200)
data = resp.get_json()
test("evidence metadata is bounded to the expected fields", set(data.keys()) == {"id", "title", "category", "severity", "source", "confidence", "evidence_count", "evidence"})

resp = client.get("/api/operations/evidence/does-not-exist")
test("404 for an unknown finding id", resp.status_code == 404)


print("\n\U0001f9ea Test 6: GET/POST /api/operations/handoff")
resp = client.get("/api/operations/handoff")
test("GET handoff -> 200", resp.status_code == 200)
data = resp.get_json()
test("handoff has the expected top-level shape", {"open_items", "new_since_prior", "snoozed_items", "capacity_watch", "pending_approvals", "recent_changes", "source_gaps", "content_hash"}.issubset(data.keys()))

resp = client.post("/api/operations/handoff", json={})
test("POST handoff missing created_by -> 400", resp.status_code == 400)

resp = client.post("/api/operations/handoff", json={"created_by": "alice"})
test("POST handoff with created_by -> 201", resp.status_code == 201)
posted = resp.get_json()
test("response includes both the handoff and the persisted marker", "handoff" in posted and "persisted" in posted)

resp = client.post("/api/operations/handoff", json={"subs": [123]})
test("POST handoff with a non-string subs entry -> 400", resp.status_code == 400)


print("\n\U0001f9ea Test 7: existing APIs are preserved unchanged")
resp = client.get("/api/health")
test("/api/health still returns 200", resp.status_code == 200)
test("/api/health body still has status/version/profile", {"status", "version", "profile"}.issubset(resp.get_json().keys()))

resp = client.get("/api/demos")
test("/api/demos still returns 200", resp.status_code == 200)


_cleanup_db()

# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
