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


print("\n\U0001f9ea Test 3b: usage_summary rolls every model call into one token ledger")
usage2 = result2["usage_summary"]
test("debate run counts 2 specialists + 2 rebuttals + 1 synthesis = 5 model calls", usage2["model_calls"] == 5)
test("total_tokens sums every call (5 x 2)", usage2["total_tokens"] == 10)
test("per-round split is specialists/rebuttals/synthesis", usage2["per_round_tokens"] == {"specialists": 4, "rebuttals": 4, "synthesis": 2})
test("orchestrator tokens attributed to the orchestrator", usage2["per_agent_tokens"]["orchestrator"] == 2)
test("tokens_per_cited_finding uses validated citations", usage2["cited_finding_count"] == 1 and usage2["tokens_per_cited_finding"] == 10)
test("final payload carries the synthesis call's own usage", result2["final"]["usage"]["total_tokens"] == 2)
usage1 = result1["usage_summary"]
test("single-specialist run: 1 call, no synthesis round double-counted", usage1["model_calls"] == 1 and "synthesis" not in usage1["per_round_tokens"])

class _ToolOutcome:
    usage = {"total_tokens": 30, "tool_calls": 3, "tool_rounds": 2}
tool_usage = analysis_mod._usage_summary({"specialists": {"scout": _ToolOutcome()}}, cited_finding_count=0)
test("Foundry tool rounds count as extra model requests (1 + 2)", tool_usage["model_calls"] == 3)
test("tool_calls summed; tokens_per_cited_finding is None with no citations", tool_usage["tool_calls"] == 3 and tool_usage["tokens_per_cited_finding"] is None)


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
test("usage_summary is present and zero (no model calls)", result5["usage_summary"]["model_calls"] == 0 and result5["usage_summary"]["total_tokens"] == 0)


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


def _activity_ids(result):
    activity = result.get("activity") if isinstance(result, dict) else None
    if not isinstance(activity, dict):
        return None
    if not activity.get("investigation_id") or not activity.get("run_id"):
        return None
    return activity


print("\n\U0001f9ea Test 10: synchronous analysis keeps its shape and records activity proof")
os.environ["OPERATIONS_STATE_DB"] = DB_PATH + ".activity"
try:
    import app.activity.store as activity_store_mod
    activity_store_mod._STORE = None
except ModuleNotFoundError:
    activity_store_mod = None

kept = {"question", "routing", "evidence_bundle", "specialists", "final", "evaluation", "model_metadata", "usage_summary"}
direct = analysis_mod.analyze_operations(
    question="what should we do?", subscription_ids=["sub-test-1"], backend=backend1, snapshot=snapshot1,
)
test("direct analyze keeps the synchronous keys", kept.issubset(direct))
direct_activity = _activity_ids(direct)
test("direct analyze adds activity investigation_id and run_id", direct_activity is not None)
test("direct activity does not replace usage_summary", "usage_summary" in direct and isinstance(direct["usage_summary"], dict))

empty_snapshot = build_snapshot([], db_path=DB_PATH + ".activity")
zero = analysis_mod.analyze_operations(
    question="anything?", subscription_ids=["sub-test-1"], backend=backend1, snapshot=empty_snapshot,
)
test("zero-evidence response keeps usage_summary and final", "usage_summary" in zero and zero["final"]["schema_valid"] is True)
zero_activity = _activity_ids(zero)
test("zero-evidence path still records activity", zero_activity is not None)
if activity_store_mod and zero_activity:
    row = activity_store_mod.get_activity_store().get_public_investigation(zero_activity["investigation_id"])
    runs = (row or {}).get("runs") or []
    match = next((item for item in runs if item.get("id") == zero_activity["run_id"]), None)
    test("zero-evidence run actual_backend is none", bool(match) and match.get("actual_backend") == "none")
    test("zero-evidence run usage is null, not invented zeros", bool(match) and match.get("usage") is None)
    test("zero-evidence run status is insufficient_evidence", bool(match) and match.get("status") == "insufficient_evidence")
    test("zero-evidence investigation stays analysis_ready, not operator closure", bool(row) and row["investigation"]["phase"] == "analysis_ready")
    event_kinds = {event.get("kind") for event in (row or {}).get("events") or []}
    test("zero-evidence run cannot be proposed", "proposal_created" not in event_kinds and "decision_recorded" not in event_kinds)
else:
    test("zero-evidence run actual_backend is none", False)
    test("zero-evidence run usage is null, not invented zeros", False)
    test("zero-evidence run status is insufficient_evidence", False)
    test("zero-evidence investigation stays analysis_ready, not operator closure", False)
    test("zero-evidence run cannot be proposed", False)

invalid = analysis_mod.analyze_operations(
    question="q", subscription_ids=["sub-test-1"], backend=BrokenBackend(), snapshot=snapshot1,
)
test("invalid schema stays schema_valid false", invalid["final"]["schema_valid"] is False)
invalid_activity = _activity_ids(invalid)
test("invalid schema still records activity", invalid_activity is not None)
if activity_store_mod and invalid_activity:
    row = activity_store_mod.get_activity_store().get_public_investigation(invalid_activity["investigation_id"])
    runs = (row or {}).get("runs") or []
    match = next((item for item in runs if item.get("id") == invalid_activity["run_id"]), None)
    test("invalid schema run status is invalid_output", bool(match) and match.get("status") == "invalid_output")
    kinds = {event.get("kind") for event in (row or {}).get("events") or []}
    test("invalid schema is not promoted to analysis_finished-only success", "analysis_failed" in kinds or (match and match.get("status") == "invalid_output"))
    test("invalid schema does not emit executed or verified", not any(
        event.get("provenance") in ("executed", "verified") for event in (row or {}).get("events") or []
    ))
else:
    test("invalid schema run status is invalid_output", False)
    test("invalid schema is not promoted to analysis_finished-only success", False)
    test("invalid schema does not emit executed or verified", False)

brief = analysis_mod.build_briefing(subscription_ids=["sub-test-1"], backend=backend2, snapshot=snapshot2)
test("briefing keeps coordinator and usage_summary", "coordinator" in brief and "usage_summary" in brief)
test("briefing adds activity ids", _activity_ids(brief) is not None)


class ToolProofBackend:
    name = "foundry_agent_service"

    def complete(self, agent_config, messages, *, json_schema=None, schema_name="", tool_context=None):
        completion = FakeCompletion(json.dumps({
            "conclusion": f"{agent_config.key} conclusion", "business_impact": "impact", "confidence": "high",
            "evidence_ids": [backend1.evidence_id], "missing_evidence": [],
            "recommended_actions": [
                {"description": "Review only", "owner": "sre", "urgency": "monitor", "approval_required": True},
            ],
            "narrative": "grounded",
        }))
        completion.backend_name = "foundry_agent_service"
        completion.provider_response_ids = ["resp-fake-1"]
        completion.tool_receipts = [{
            "agent_key": agent_config.key, "round": 1, "tool_name": "get_capacity_watch",
            "status": "ok", "duration_ms": 3, "result_count": 1,
        }]
        return completion


tooled = analysis_mod.analyze_operations(
    question="quota?", subscription_ids=["sub-test-1"], backend=ToolProofBackend(), snapshot=snapshot1,
)
tooled_activity = _activity_ids(tooled)
test("Foundry tool proof is recorded on the analysis activity", tooled_activity is not None)
if activity_store_mod and tooled_activity:
    row = activity_store_mod.get_activity_store().get_public_investigation(tooled_activity["investigation_id"])
    events = (row or {}).get("events") or []
    tool_events = [event for event in events if event.get("kind") == "tool_completed"]
    test("successful tool becomes a tool_completed receipt", bool(tool_events))
    model_events = [event for event in events if event.get("kind") == "model_completed"]
    test("model_completed event is recorded", bool(model_events))
    test("model_completed provenance is observed", bool(model_events) and all(event.get("provenance") == "observed" for event in model_events))
    blob = json.dumps(tool_events)
    test("recorded tool receipt has no arguments or output", "arguments" not in blob and "subscription_ids" not in blob)
    test("recorded tool receipt names the tool", "get_capacity_watch" in blob)
else:
    test("successful tool becomes a tool_completed receipt", False)
    test("model_completed event is recorded", False)
    test("model_completed provenance is observed", False)
    test("recorded tool receipt has no arguments or output", False)
    test("recorded tool receipt names the tool", False)

_cleanup_db(DB_PATH + ".activity")
_cleanup_db()
_cleanup_db(DB_PATH + ".2")
evaluation_mod.reset_for_tests()

# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
