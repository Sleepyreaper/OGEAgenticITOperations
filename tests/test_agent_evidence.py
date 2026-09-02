#!/usr/bin/env python3
"""Test build_evidence_bundle (app/agents/evidence.py) -- bounds,
redaction (never a subscription id/raw_excerpt reaches the bundle),
filtering, and the finding_id not-found error.

Run: python3 tests/test_agent_evidence.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-test-1"
DB_PATH = str(REPO_ROOT / "tests" / "_test_agent_evidence.db")


def _cleanup_db():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup_db()

from app.agents.evidence import (  # noqa: E402
    MAX_EVIDENCE_PER_ITEM, MAX_ITEMS_CEILING, EvidenceBundleError, build_evidence_bundle,
)
from app.operations.models import (  # noqa: E402
    ConfidenceLevel, EvidenceReference, EvidenceSource, Finding, FindingCategory, FindingStatus, Severity,
)
from app.operations.priority import prioritize_findings  # noqa: E402
from app.operations.snapshot import OperationsSnapshot  # noqa: E402
from app.operations.state import OperationsStateStore, merge_workflow_state  # noqa: E402

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


SENSITIVE_SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"


def make_finding(category, disc, *, severity=Severity.HIGH.value, exec_att=False, evidence_count=1):
    evidence = [
        EvidenceReference(
            source=EvidenceSource.RESOURCE_GRAPH.value, title=f"evidence-{i}", observed_at="2025-06-01T00:00:00Z",
            resource_id=f"/subscriptions/{SENSITIVE_SUBSCRIPTION_ID}/resourceGroups/rg/providers/x/{i}",
            raw_excerpt="raw azure api dump that should never reach a model prompt",
        )
        for i in range(evidence_count)
    ]
    return Finding(
        category=category, severity=severity, status=FindingStatus.OPEN.value,
        title=f"Finding {disc}", summary="s", business_impact="b",
        first_seen="2025-06-01T00:00:00Z", last_seen="2025-06-01T00:00:00Z",
        source=EvidenceSource.RESOURCE_GRAPH.value, confidence=ConfidenceLevel.DERIVED.value,
        evidence=evidence, executive_attention=exec_att, discriminator=disc,
    )


def build_snapshot(findings, *, db_path=DB_PATH):
    prioritized = prioritize_findings(findings)
    store = OperationsStateStore(db_path)
    merged = merge_workflow_state([pf.finding for pf in prioritized], store)
    merged_by_id = {m["finding"]["id"]: m for m in merged}
    ordered = []
    for pf in prioritized:
        item = merged_by_id[pf.finding.id]
        item["priority"] = {"band": pf.band, "factors": pf.factors.to_dict()}
        ordered.append(item)
    return OperationsSnapshot(
        id="snap-1", generated_at="2025-06-01T00:00:00.000Z", subscription_ids=("sub-test-1",), status="ok",
        envelopes=[], findings=ordered, coverage={"total_sources": 3, "ok_count": 3}, source_errors=[], summary={},
    )


print("\n\U0001f9ea Test 1: bounded item fields + no subscription id / raw_excerpt leak")
finding1 = make_finding(FindingCategory.COST.value, "d1", evidence_count=5)
snapshot1 = build_snapshot([finding1])
bundle1 = build_evidence_bundle(snapshot1)
bundle1_dict = bundle1.to_dict()

test("exactly one item", len(bundle1_dict["items"]) == 1)
test("evidence is bounded to MAX_EVIDENCE_PER_ITEM even though the Finding had 5", len(bundle1_dict["items"][0]["evidence"]) == MAX_EVIDENCE_PER_ITEM)
test("evidence refs have only source/title/observed_at/reference keys", set(bundle1_dict["items"][0]["evidence"][0].keys()) == {"source", "title", "observed_at", "reference"})
test("no subscription id anywhere in the serialized bundle", SENSITIVE_SUBSCRIPTION_ID not in bundle1.to_prompt_json())
test("no raw_excerpt text anywhere in the serialized bundle", "raw azure api dump" not in bundle1.to_prompt_json())
test("customer_impact is derived from priority factors", bundle1_dict["items"][0]["customer_impact"] in (True, False))
test("known_ids returns the finding id", finding1.id in bundle1.known_ids())


print("\n\U0001f9ea Test 2: max_items bounds total items and reports truncated/total_available")
many_findings = [make_finding(FindingCategory.COST.value, f"m{i}") for i in range(5)]
snapshot2 = build_snapshot(many_findings, db_path=DB_PATH + ".2")
bundle2 = build_evidence_bundle(snapshot2, max_items=2)
test("items bounded to max_items", len(bundle2.items) == 2)
test("total_available reflects the full match count", bundle2.total_available == 5)
test("truncated is True", bundle2.truncated is True)

bundle2_ceiling = build_evidence_bundle(snapshot2, max_items=9999)
test("max_items is clamped to MAX_ITEMS_CEILING even when the caller asks for more", len(bundle2_ceiling.items) <= MAX_ITEMS_CEILING)


print("\n\U0001f9ea Test 3: category/severity/status filters")
cost_finding = make_finding(FindingCategory.COST.value, "cat1")
security_finding = make_finding(FindingCategory.SECURITY.value, "cat2")
snapshot3 = build_snapshot([cost_finding, security_finding], db_path=DB_PATH + ".3")
bundle3 = build_evidence_bundle(snapshot3, category=FindingCategory.SECURITY.value)
test("category filter matches only the security finding", [i.id for i in bundle3.items] == [security_finding.id])

try:
    build_evidence_bundle(snapshot3, category="not-a-real-category")
    test("unknown category raises EvidenceBundleError", False)
except EvidenceBundleError:
    test("unknown category raises EvidenceBundleError", True)


print("\n\U0001f9ea Test 4: finding_id -- exact match or an explicit not-found error")
bundle4 = build_evidence_bundle(snapshot3, finding_id=cost_finding.id)
test("finding_id filter returns exactly that finding", len(bundle4.items) == 1 and bundle4.items[0].id == cost_finding.id)

try:
    build_evidence_bundle(snapshot3, finding_id="does-not-exist")
    test("unknown finding_id raises EvidenceBundleError (never an empty-but-silent bundle)", False)
except EvidenceBundleError as exc:
    test("unknown finding_id raises EvidenceBundleError (never an empty-but-silent bundle)", "not found" in str(exc))


print("\n\U0001f9ea Test 5: an empty (filtered-to-nothing) bundle is a valid, explicit result")
bundle5 = build_evidence_bundle(snapshot3, category=FindingCategory.BACKUP.value)
test("no matches is an empty (not an error) bundle", bundle5.items == () and bundle5.total_available == 0)


for suffix in ("", ".2", ".3"):
    for ext in ("", "-wal", "-shm"):
        p = DB_PATH + suffix + ext
        if os.path.exists(p):
            os.remove(p)

# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
