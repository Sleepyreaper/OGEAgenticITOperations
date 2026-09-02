#!/usr/bin/env python3
"""Test the grounded-analysis orchestrator (app/agents/analysis.py) end
to end with a mocked backend and a hand-built OperationsSnapshot -- no
real Azure/model calls. Covers routine vs. debate routing, citation
validation (including unsupported citations), task adherence (an action
is never auto_executable), the zero-model-call insufficient-evidence
path, and requested-agent validation.

Run: python3 tests/test_agent_analysis.py
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-test-1"
DB_PATH = str(REPO_ROOT / "tests" / "_test_agent_analysis.db")


def _cleanup_db(path=DB_PATH):
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup_db()

from app.agents import analysis as analysis_mod  # noqa: E402
from app.agents import evaluation as evaluation_mod  # noqa: E402
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


def make_finding(category, severity, disc, *, exec_att=False):
    return Finding(
        category=category, severity=severity, status=FindingStatus.OPEN.value,
        title=f"{category} issue {disc}", summary="s", business_impact="b",
        first_seen="2025-06-01T00:00:00Z", last_seen="2025-06-01T00:00:00Z",
        source=EvidenceSource.RESOURCE_GRAPH.value, confidence=ConfidenceLevel.DERIVED.value,
        evidence=[EvidenceReference(source=EvidenceSource.RESOURCE_GRAPH.value, title="t", observed_at="2025-06-01T00:00:00Z")],
        executive_attention=exec_att, discriminator=disc,
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
        envelopes=[], findings=ordered, coverage={"total_sources": 1, "ok_count": 1}, source_errors=[], summary={},
    )


class FakeCompletion:
    def __init__(self, raw_text, structured_output_used=True):
        self.raw_text = raw_text
        self.structured_output_used = structured_output_used
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "estimated_cost_usd": 0.0}
        self.finish_reason = "stop"


class FakeBackend:
    """Returns a valid structured payload citing `evidence_id`, tracking
    every agent_key it was called for."""

    name = "fake"

    def __init__(self, evidence_id, action_description="Restart the dev VM"):
        self.evidence_id = evidence_id
        self.action_description = action_description
        self.calls = []

    def complete(self, agent_config, messages, *, json_schema=None, schema_name=""):
        self.calls.append(agent_config.key)
        payload = {
            "conclusion": f"{agent_config.key} conclusion", "business_impact": "impact", "confidence": "high",
            "evidence_ids": [self.evidence_id], "missing_evidence": [],
            "recommended_actions": [
                {"description": self.action_description, "owner": "sre", "urgency": "immediate", "approval_required": True},
            ],
            "narrative": "because the evidence bundle says so",
        }
        return FakeCompletion(json.dumps(payload))


class BrokenBackend:
    """Always returns malformed (non-JSON) text."""

    name = "broken"

    def complete(self, agent_config, messages, *, json_schema=None, schema_name=""):
        return FakeCompletion("I cannot help with that.")


evaluation_mod.reset_for_tests()


print("\n\U0001f9ea Test 1: routine single-domain, single finding -> ONE specialist, no coordinator, no debate")
finding1 = make_finding(FindingCategory.COST.value, Severity.LOW.value, "c1")
snapshot1 = build_snapshot([finding1])
backend1 = FakeBackend(finding1.id)
result1 = analysis_mod.analyze_operations(question="what's going on", subscription_ids=["sub-test-1"], backend=backend1, snapshot=snapshot1)
test("routing selected exactly one specialist", result1["routing"]["specialist_agents"] == ["cost_sentinel"])
test("no coordinator call", result1["routing"]["coordinator_included"] is False)
test("only one backend call was made", backend1.calls == ["cost_sentinel"])
test("final result is schema_valid", result1["final"]["schema_valid"] is True)
test("cited evidence id is recognized as valid (no unsupported citations)", result1["final"]["unsupported_evidence_ids"] == [])
test("evaluation reports 100% citation validity", result1["evaluation"]["citation_validity_pct"] == 100.0)


print("\n\U0001f9ea Test 2: task adherence -- EVERY recommended action is auto_executable: False")
action = result1["final"]["recommended_actions"][0]
test("action carries deterministic approval metadata", "approval" in action)
test("action is never marked auto_executable, regardless of the model's own approval_required", action["approval"]["auto_executable"] is False)


print("\n\U0001f9ea Test 3: cross-domain evidence triggers debate + coordinator + rebuttal round")
finding2a = make_finding(FindingCategory.COST.value, Severity.LOW.value, "c2")
finding2b = make_finding(FindingCategory.SECURITY.value, Severity.LOW.value, "c3")
snapshot2 = build_snapshot([finding2a, finding2b], db_path=DB_PATH + ".2")
backend2 = FakeBackend(finding2a.id)
result2 = analysis_mod.analyze_operations(question="q", subscription_ids=["sub-test-1"], backend=backend2, snapshot=snapshot2)
test("routing debate is True", result2["routing"]["debate"] is True)
test("both specialists + orchestrator were called", set(backend2.calls) == {"cost_sentinel", "scout", "orchestrator"})
test("rebuttals are present (2+ specialists in a debate)", result2["rebuttals"] is not None)
test("final synthesis came from the orchestrator", result2["final"]["agent_key"] == "orchestrator")


print("\n\U0001f9ea Test 4: unsupported citation is flagged, never silently accepted")
backend3 = FakeBackend("this-id-does-not-exist-in-the-bundle")
result3 = analysis_mod.analyze_operations(question="q", subscription_ids=["sub-test-1"], backend=backend3, snapshot=snapshot1)
test("unsupported_evidence_ids surfaces the bad citation", result3["final"]["unsupported_evidence_ids"] == ["this-id-does-not-exist-in-the-bundle"])
test("valid_evidence_ids is empty", result3["final"]["valid_evidence_ids"] == [])
test("evaluation counts exactly one unsupported citation", result3["evaluation"]["unsupported_citation_count"] == 1)


print("\n\U0001f9ea Test 5: malformed model output is an explicit failure, never a fabricated answer")
result4 = analysis_mod.analyze_operations(question="q", subscription_ids=["sub-test-1"], backend=BrokenBackend(), snapshot=snapshot1)
test("final.schema_valid is False", result4["final"]["schema_valid"] is False)
test("a schema_error is present", bool(result4["final"]["schema_error"]))
test("no 'conclusion'/'confidence' fields are fabricated on a schema_valid=False result", "conclusion" not in result4["final"])
test("evaluation reports schema_valid False for this analysis", result4["evaluation"]["schema_valid"] is False)


print("\n\U0001f9ea Test 6: zero matching evidence -> deterministic answer, ZERO model calls")
backend5 = FakeBackend(finding1.id)
result5 = analysis_mod.analyze_operations(question="q", subscription_ids=["sub-test-1"], category=FindingCategory.BACKUP.value, backend=backend5, snapshot=snapshot1)
test("no backend calls were made", backend5.calls == [])
test("specialists dict is empty", result5["specialists"] == {})
test("final is still schema_valid (a deterministic, not fabricated, answer)", result5["final"]["schema_valid"] is True)
test("confidence is explicitly low", result5["final"]["confidence"] == "low")


print("\n\U0001f9ea Test 7: request-level validation -- blank question, no subscription, unknown/orchestrator agent")
try:
    analysis_mod.analyze_operations(question="  ", subscription_ids=["sub-test-1"], backend=backend1, snapshot=snapshot1)
    test("blank question raises AnalysisError", False)
except analysis_mod.AnalysisError:
    test("blank question raises AnalysisError", True)

try:
    analysis_mod.analyze_operations(question="q", subscription_ids=[], backend=backend1, snapshot=snapshot1)
    test("empty subscription_ids raises AnalysisError", False)
except analysis_mod.AnalysisError:
    test("empty subscription_ids raises AnalysisError", True)

try:
    analysis_mod.analyze_operations(question="q", subscription_ids=["sub-test-1"], requested_agents=["orchestrator"], backend=backend1, snapshot=snapshot1)
    test("requesting 'orchestrator' as a specialist raises AnalysisError (it's the coordinator, not a specialist)", False)
except analysis_mod.AnalysisError:
    test("requesting 'orchestrator' as a specialist raises AnalysisError (it's the coordinator, not a specialist)", True)

try:
    analysis_mod.analyze_operations(question="q", subscription_ids=["sub-test-1"], requested_agents=["not_a_real_agent"], backend=backend1, snapshot=snapshot1)
    test("an unknown agent key raises AnalysisError", False)
except analysis_mod.AnalysisError:
    test("an unknown agent key raises AnalysisError", True)


print("\n\U0001f9ea Test 8: unknown finding_id propagates as EvidenceBundleError, not a silent empty result")
try:
    analysis_mod.analyze_operations(question="q", subscription_ids=["sub-test-1"], finding_id="does-not-exist", backend=backend1, snapshot=snapshot1)
    test("unknown finding_id raises EvidenceBundleError", False)
except analysis_mod.EvidenceBundleError:
    test("unknown finding_id raises EvidenceBundleError", True)


print("\n\U0001f9ea Test 9: build_briefing collapses specialist detail to a bullet -- one coordinator voice")
result6 = analysis_mod.build_briefing(subscription_ids=["sub-test-1"], backend=backend2, snapshot=snapshot2)
test("briefing exposes exactly one 'coordinator' answer", result6["coordinator"]["agent_key"] == "orchestrator")
test("supporting_analysis entries are bounded to agent/role/confidence/conclusion (no narrative/raw text)", all(set(item) == {"agent_key", "agent", "role", "schema_valid", "confidence", "conclusion"} for item in result6["supporting_analysis"]))


_cleanup_db()
_cleanup_db(DB_PATH + ".2")
evaluation_mod.reset_for_tests()

# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
