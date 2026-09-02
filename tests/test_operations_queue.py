#!/usr/bin/env python3
"""Test the unified operations queue (app/operations/queue.py) --
filter/sort/paginate over an already-prioritized findings list, rank
transparency (rank_reason), and strict filter/pagination validation.

Run: python3 tests/test_operations_queue.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.models import (  # noqa: E402
    ConfidenceLevel, EvidenceReference, EvidenceSource, Finding, FindingCategory, FindingStatus, Severity,
)
from app.operations.priority import prioritize_findings  # noqa: E402
from app.operations.queue import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, QueueValidationError, build_queue  # noqa: E402

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


def make_finding(disc, *, category=FindingCategory.SECURITY.value, severity=Severity.HIGH.value, exec_att=False, approval=False, owner=""):
    return Finding(
        category=category, severity=severity, status=FindingStatus.OPEN.value,
        title=f"Finding {disc}", summary="s", business_impact="b",
        first_seen="2025-06-01T00:00:00Z", last_seen="2025-06-01T00:00:00Z",
        source=EvidenceSource.RESOURCE_GRAPH.value, confidence=ConfidenceLevel.CONFIRMED.value, owner=owner,
        evidence=[EvidenceReference(source=EvidenceSource.RESOURCE_GRAPH.value, title="t", observed_at="2025-06-01T00:00:00Z")],
        executive_attention=exec_att, approval_required=approval, discriminator=disc,
    )


def wrap_all(findings):
    """Mirror app.operations.snapshot's {"finding","workflow","priority"}
    shape using the real prioritize_findings() so ordering matches
    production exactly."""
    prioritized = prioritize_findings(findings)
    items = []
    for pf in prioritized:
        items.append({
            "finding": pf.finding.to_dict(),
            "workflow": {"status": "new", "assigned_owner": "", "disposition_reason": "", "snooze_until": None, "first_seen_at": None, "created_at": None, "updated_at": None},
            "priority": {"band": pf.band, "factors": pf.factors.to_dict()},
        })
    return items


print("\n\U0001f9ea Test 1: build_queue -- preserves the already-computed priority order")
critical = make_finding("c1", severity=Severity.CRITICAL.value, exec_att=True)
low = make_finding("l1", severity=Severity.LOW.value)
items = wrap_all([low, critical])
result = build_queue(items)
test("2 items returned", result["total"] == 2)
test("critical-severity finding ranks first", result["items"][0]["id"] == critical.id)
test("rank/rank_of are 1-indexed and correct", result["items"][0]["rank"] == 1 and result["items"][0]["rank_of"] == 2)
test("rank_reason mentions the severity rank", "severity_rank=0" in result["items"][0]["rank_reason"])
test("priority_band is exposed", result["items"][0]["priority_band"] == "P1")


print("\n\U0001f9ea Test 2: build_queue -- filter by category/severity/status/owner")
sec = make_finding("sec-1", category=FindingCategory.SECURITY.value, severity=Severity.HIGH.value)
cost = make_finding("cost-1", category=FindingCategory.COST.value, severity=Severity.LOW.value, owner="team-a")
items2 = wrap_all([sec, cost])
items2[1]["workflow"]["assigned_owner"] = "team-a"

result_cat = build_queue(items2, category=FindingCategory.COST.value)
test("category filter returns only the matching finding", result_cat["total"] == 1 and result_cat["items"][0]["id"] == cost.id)

result_sev = build_queue(items2, severity=Severity.HIGH.value)
test("severity filter returns only the matching finding", result_sev["total"] == 1 and result_sev["items"][0]["id"] == sec.id)

result_owner = build_queue(items2, owner="TEAM-A")
test("owner filter is case-insensitive and matches assigned_owner", result_owner["total"] == 1 and result_owner["items"][0]["id"] == cost.id)

result_status = build_queue(items2, status="new")
test("workflow status filter matches all (both are 'new')", result_status["total"] == 2)


print("\n\U0001f9ea Test 3: build_queue -- pagination")
many = [make_finding(f"m{i}", severity=Severity.MEDIUM.value) for i in range(30)]
items3 = wrap_all(many)
page1 = build_queue(items3, page=1, page_size=10)
test("page 1 has 10 items", len(page1["items"]) == 10)
test("total_pages is 3", page1["total_pages"] == 3)
page2 = build_queue(items3, page=2, page_size=10)
test("page 2 starts at rank 11", page2["items"][0]["rank"] == 11)
test("page 1 and page 2 don't overlap", not ({i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]}))
default_page = build_queue(items3)
test("default page_size matches DEFAULT_PAGE_SIZE", len(default_page["items"]) == min(DEFAULT_PAGE_SIZE, 30))


print("\n\U0001f9ea Test 4: build_queue -- strict validation, never silently ignored")
try:
    build_queue(items3, category="not-a-real-category")
    test("an unrecognized category raises QueueValidationError", False)
except QueueValidationError:
    test("an unrecognized category raises QueueValidationError", True)

try:
    build_queue(items3, severity="apocalyptic")
    test("an unrecognized severity raises QueueValidationError", False)
except QueueValidationError:
    test("an unrecognized severity raises QueueValidationError", True)

try:
    build_queue(items3, status="not-a-real-status")
    test("an unrecognized status raises QueueValidationError", False)
except QueueValidationError:
    test("an unrecognized status raises QueueValidationError", True)

try:
    build_queue(items3, page=0)
    test("page=0 raises QueueValidationError", False)
except QueueValidationError:
    test("page=0 raises QueueValidationError", True)

try:
    build_queue(items3, page_size=MAX_PAGE_SIZE + 1)
    test("page_size beyond MAX_PAGE_SIZE raises QueueValidationError", False)
except QueueValidationError:
    test("page_size beyond MAX_PAGE_SIZE raises QueueValidationError", True)

test("Finding.status values are also accepted as a status filter (distinct vocabulary)", build_queue(items3, status="open")["total"] == 30)


print("\n\U0001f9ea Test 5: build_queue -- evidence is bounded (max 5) and evidence_count reflects the true total")
many_evidence_finding = make_finding("many-ev", severity=Severity.HIGH.value)
item = wrap_all([many_evidence_finding])[0]
item["finding"]["evidence"] = [{"source": "resource_graph", "title": f"e{i}", "observed_at": "2025-06-01T00:00:00.000Z", "resource_id": None, "reference": None, "raw_excerpt": None} for i in range(8)]
result5 = build_queue([item])
test("evidence_count reflects the true total (8)", result5["items"][0]["evidence_count"] == 8)
test("evidence list itself is bounded to 5", len(result5["items"][0]["evidence"]) == 5)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
