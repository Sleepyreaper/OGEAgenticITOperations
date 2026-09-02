#!/usr/bin/env python3
"""Test the centralized Demo-mode fixture (app/operations/demo_fixture.py)
and its route (GET /api/operations/demo, app/operations/routes.py) --
schema shape, honesty (never a fabricated all-clear/coverage), the
scripted shift-handoff story (new/changed/snoozed/pending approvals/
source gaps), evidence-id grounding for the two "simulated" AI examples,
and that the disposable SQLite state store it uses leaves nothing behind
on disk.

Run: python3 tests/test_operations_demo_fixture.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)

from app.approval import ApprovalTier  # noqa: E402
from app.operations.demo_fixture import build_demo_payload  # noqa: E402
from app.operations.models import parse_utc_iso  # noqa: E402

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


print("\n\U0001f9ea Test 1: build_demo_payload() top-level schema")
payload = build_demo_payload()
test("has meta/snapshot/brief/queue/handoff/analysis_example/briefing_example keys",
     set(payload.keys()) == {"meta", "snapshot", "brief", "queue", "handoff", "analysis_example", "briefing_example"})
test("meta.demo is True (never disguised as live)", payload["meta"]["demo"] is True)
test("meta.label is a non-empty string", isinstance(payload["meta"]["label"], str) and len(payload["meta"]["label"]) > 20)
test("meta.generated_at parses as a valid UTC ISO timestamp", parse_utc_iso(payload["meta"]["generated_at"]) is not None)
hero_id = payload["meta"]["hero_finding_id"]
test("meta.hero_finding_id is a non-empty string", isinstance(hero_id, str) and hero_id)

print("\n\U0001f9ea Test 2: snapshot section is honest -- partial coverage, never faked as all-clear")
snap = payload["snapshot"]
test("snapshot.status is 'partial' (one error + one not_configured source were seeded)", snap["status"] == "partial")
test("coverage.error_count >= 1", snap["coverage"]["error_count"] >= 1)
test("coverage.not_configured_count >= 1", snap["coverage"]["not_configured_count"] >= 1)
test("coverage.ok_count + error_count + not_configured_count == total_sources",
     snap["coverage"]["ok_count"] + snap["coverage"]["error_count"] + snap["coverage"]["not_configured_count"]
     + snap["coverage"]["not_supported_count"] == snap["coverage"]["total_sources"])
test("summary.total_findings == 13 (one per FindingCategory)", snap["summary"]["total_findings"] == 13)

print("\n\U0001f9ea Test 3: brief section -- no fabricated composite score, honest reliability/capacity")
brief = payload["brief"]
test("overall_state is a recognized value", brief["overall_state"] in ("healthy", "attention", "impact", "unknown"))
test("headline is non-empty", bool(brief["headline"]))
test("no fabricated 'readiness_score'/'uptime'/'revenue_at_risk'/'mttr' key anywhere in the brief",
     not any(k in brief for k in ("readiness_score", "uptime", "revenue_at_risk", "mttr")))
test("reliability.slo_configured is True (workload_slo envelope was seeded ok)", brief["reliability"]["slo_configured"] is True)
test("reliability.state is 'at_risk' (matches the seeded SLOSummary)", brief["reliability"]["state"] == "at_risk")
test("capacity.configured is True (capacity envelope was seeded ok)", brief["capacity"]["configured"] is True)
test("capacity.state is 'critical' (matches the seeded CapacitySummary)", brief["capacity"]["state"] == "critical")
test("business_impact.active_customer_impacting_count > 0 (hero + others are exec-attention/high-severity)",
     brief["business_impact"]["active_customer_impacting_count"] > 0)

decisions_required_titles = {d["title"] for d in brief["decisions_required"]}
test(
    "decisions_required excludes the routine, Medium-severity, non-executive compliance approval "
    "(human approval alone is not executive attention)",
    "Storage account stgdemoreports01 fails 'require-private-endpoint' policy" not in decisions_required_titles,
)
test(
    "decisions_required still includes the Critical-severity hero security approval",
    "NSG 'nsg-web-pe' allows inbound SSH (22) from any source" in decisions_required_titles,
)
test(
    "decisions_required still includes the High-severity capacity quota approval",
    "Standard Dv5 vCPU quota in eastus2 at 95% utilization" in decisions_required_titles,
)
queue_titles = {item["title"] for item in payload["queue"]["items"]}
test(
    "the excluded compliance approval remains fully visible in the Ops queue (never hidden entirely)",
    "Storage account stgdemoreports01 fails 'require-private-endpoint' policy" in queue_titles,
)

print("\n\U0001f9ea Test 4: queue section")
queue = payload["queue"]
test("queue.total == 13", queue["total"] == 13)
test("every queue item has priority_band/rank/rank_reason/evidence_count", all(
    {"priority_band", "rank", "rank_reason", "evidence_count", "workflow_status"} <= set(item.keys()) for item in queue["items"]
))
test("hero finding is present and ranked #1 (P1 critical, newest)", queue["items"][0]["id"] == hero_id and queue["items"][0]["priority_band"] == "P1")

print("\n\U0001f9ea Test 5: handoff section -- the scripted shift story (real state-machine + real handoff diffing, never hand-faked)")
handoff = payload["handoff"]
test("exactly 3 new_since_prior (hero + change + compliance findings)", len(handoff["new_since_prior"]) == 3)
test("exactly 2 changed_since_prior (reliability + backup, updated after the prior handoff)", len(handoff["changed_since_prior"]) == 2)
test("exactly 1 snoozed_items (patch finding)", len(handoff["snoozed_items"]) == 1)
test("pending_approvals is non-empty (hero/capacity/compliance all approval_required)", len(handoff["pending_approvals"]) >= 3)
test("source_gaps includes the seeded error + not_configured sources", len(handoff["source_gaps"]) == 2)
test("open_item_count excludes the dismissed and snoozed findings (13 - 1 dismissed - 1 snoozed = 11)", handoff["open_item_count"] == 11)

print("\n\U0001f9ea Test 6: analysis_example -- simulated narrative, but real routing/evidence/approval grounding")
analysis = payload["analysis_example"]
test("marked simulated: true", analysis["simulated"] is True)
test("final.evidence_ids references only the hero finding (bounded to what was asked)", analysis["final"]["evidence_ids"] == [hero_id])
test("final.valid_evidence_ids is non-empty and unsupported_evidence_ids is empty (real citation validation ran)",
     analysis["final"]["valid_evidence_ids"] == [hero_id] and analysis["final"]["unsupported_evidence_ids"] == [])
valid_tiers = {t.value for t in ApprovalTier}
test("every recommended action carries a real ApprovalTier via app.approval.analysis_action_metadata", all(
    a["approval"]["tier"] in valid_tiers for a in analysis["final"]["recommended_actions"]
))
test("routing.specialist_agents is non-empty (app.agents.routing.route ran for real)", len(analysis["routing"]["specialist_agents"]) > 0)

print("\n\U0001f9ea Test 7: briefing_example -- one coordinator voice + collapsed supporting analysis")
briefing = payload["briefing_example"]
test("marked simulated: true", briefing["simulated"] is True)
test("coordinator.conclusion is non-empty", bool(briefing["coordinator"]["conclusion"]))
test("supporting_analysis is a list (collapsed specialist bullets, not shown by default in the UI)", isinstance(briefing["supporting_analysis"], list))

print("\n\U0001f9ea Test 8: the disposable SQLite state store leaves nothing behind on disk")
build_demo_payload()  # second call -- must not collide with / leak the first call's temp file
leftovers = [f for f in os.listdir(str(REPO_ROOT)) if f.startswith(".ops_demo_fixture_")]
test("no .ops_demo_fixture_* files remain in the repo root after two calls", leftovers == [])

print(f"\n{'='*60}\nResults: {PASS} passed, {FAIL} failed\n{'='*60}")
sys.exit(1 if FAIL else 0)
