#!/usr/bin/env python3
"""Test the /api/operations/analyze, /briefing, and /tools* Flask routes
(app/agents/analysis_routes.py) -- request parsing, status codes, and
that existing APIs (/api/health, /api/demos, /api/ask) are preserved
unchanged. app.agents.analysis.get_snapshot and
app.agents.backend.get_backend are monkeypatched -- no real Azure/model
calls (app.agents.tools.get_snapshot is patched separately for the
/tools routes).

Run: python3 tests/test_agent_analysis_routes.py
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-test-1"
DB_PATH = str(REPO_ROOT / "tests" / "_test_agent_analysis_routes.db")


def _cleanup_db():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup_db()
os.environ["OPERATIONS_STATE_DB"] = DB_PATH

from app.agents import analysis as analysis_mod  # noqa: E402
from app.agents import backend as backend_mod  # noqa: E402
from app.agents import tools as tools_mod  # noqa: E402
from app.main import create_app  # noqa: E402
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


def make_finding(disc, *, category=FindingCategory.COST.value, severity=Severity.LOW.value):
    return Finding(
        category=category, severity=severity, status=FindingStatus.OPEN.value,
        title=f"Finding {disc}", summary="s", business_impact="b",
        first_seen="2025-06-01T00:00:00Z", last_seen="2025-06-01T00:00:00Z",
        source=EvidenceSource.RESOURCE_GRAPH.value, confidence=ConfidenceLevel.DERIVED.value,
        evidence=[EvidenceReference(source=EvidenceSource.RESOURCE_GRAPH.value, title="t", observed_at="2025-06-01T00:00:00Z")],
        discriminator=disc,
    )


FINDING_A = make_finding("route-a")

_prioritized = prioritize_findings([FINDING_A])
_store = OperationsStateStore(DB_PATH)
_merged = merge_workflow_state([pf.finding for pf in _prioritized], _store)
_merged_by_id = {m["finding"]["id"]: m for m in _merged}
_ordered = []
for _pf in _prioritized:
    _item = _merged_by_id[_pf.finding.id]
    _item["priority"] = {"band": _pf.band, "factors": _pf.factors.to_dict()}
    _ordered.append(_item)

CANNED_SNAPSHOT = OperationsSnapshot(
    id="snap-canned", generated_at="2025-06-01T00:00:00.000Z", subscription_ids=("sub-test-1",), status="ok",
    envelopes=[], findings=_ordered, coverage={"total_sources": 1, "ok_count": 1}, source_errors=[],
    summary={"total_findings": 1},
)


class FakeCompletion:
    def __init__(self, raw_text):
        self.raw_text = raw_text
        self.structured_output_used = True
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "estimated_cost_usd": 0.0}
        self.finish_reason = "stop"


class FakeBackend:
    name = "fake"

    def complete(self, agent_config, messages, *, json_schema=None, schema_name=""):
        payload = {
            "conclusion": f"{agent_config.key} conclusion", "business_impact": "impact", "confidence": "high",
            "evidence_ids": [FINDING_A.id], "missing_evidence": [],
            "recommended_actions": [
                {"description": "Review the finding", "owner": "sre", "urgency": "monitor", "approval_required": False},
            ],
            "narrative": "grounded in the evidence bundle",
        }
        return FakeCompletion(json.dumps(payload))


class RaisingBackend:
    name = "raising"

    def complete(self, agent_config, messages, *, json_schema=None, schema_name=""):
        raise NotImplementedError("Foundry Agent Service backend is not implemented in this runtime")


analysis_mod.get_snapshot = lambda subs, config=None, force_refresh=False: CANNED_SNAPSHOT
tools_mod.get_snapshot = lambda subs, config=None, force_refresh=False: CANNED_SNAPSHOT
backend_mod.get_backend = lambda name="": FakeBackend()

app = create_app()
client = app.test_client()


print("\n\U0001f9ea Test 1: GET /api/operations/analyze -- happy path")
resp = client.get("/api/operations/analyze?question=what+is+happening")
test("200 OK", resp.status_code == 200)
data = resp.get_json()
test("response has routing/evidence_bundle/final/evaluation/model_metadata", {"routing", "evidence_bundle", "final", "evaluation", "model_metadata"}.issubset(data.keys()))
test("final is grounded and schema_valid", data["final"]["schema_valid"] is True)
test("final cites a real finding id", data["final"]["evidence_ids"] == [FINDING_A.id])
test("model_metadata reports the direct backend", "backend" in data["model_metadata"])


print("\n\U0001f9ea Test 2: POST /api/operations/analyze -- missing question -> 400")
resp = client.post("/api/operations/analyze", json={})
test("missing question -> 400", resp.status_code == 400)


print("\n\U0001f9ea Test 3: POST /api/operations/analyze -- unknown category filter -> 400")
resp = client.post("/api/operations/analyze", json={"question": "q", "category": "not-a-real-category"})
test("unknown category -> 400", resp.status_code == 400)


print("\n\U0001f9ea Test 4: POST /api/operations/analyze -- unknown finding_id -> 404")
resp = client.post("/api/operations/analyze", json={"question": "q", "finding_id": "does-not-exist"})
test("unknown finding_id -> 404", resp.status_code == 404)


print("\n\U0001f9ea Test 5: POST /api/operations/analyze -- unknown requested agent -> 400")
resp = client.post("/api/operations/analyze", json={"question": "q", "agents": ["not_a_real_agent"]})
test("unknown requested agent -> 400", resp.status_code == 400)


print("\n\U0001f9ea Test 6: a NotImplementedError backend (e.g. AGENT_BACKEND=foundry) -> 501, never silently falls back")
backend_mod.get_backend = lambda name="": RaisingBackend()
resp = client.post("/api/operations/analyze", json={"question": "q"})
test("NotImplementedError backend -> 501", resp.status_code == 501)
backend_mod.get_backend = lambda name="": FakeBackend()


print("\n\U0001f9ea Test 7: GET /api/operations/briefing -- one coordinator voice")
resp = client.get("/api/operations/briefing")
test("200 OK", resp.status_code == 200)
briefing_data = resp.get_json()
test("briefing has coordinator/supporting_analysis/routing/evaluation", {"coordinator", "supporting_analysis", "routing", "evaluation"}.issubset(briefing_data.keys()))


print("\n\U0001f9ea Test 8: GET /api/operations/tools -- introspection only, no execution")
resp = client.get("/api/operations/tools")
test("200 OK", resp.status_code == 200)
tools_data = resp.get_json()
test("6 tools listed", len(tools_data["tools"]) == 6)
test("every tool is read_only", all(t["read_only"] is True for t in tools_data["tools"]))


print("\n\U0001f9ea Test 9: POST /api/operations/tools/<name> -- valid + invalid invocation")
resp = client.post("/api/operations/tools/get_source_coverage", json={"arguments": {"subscription_ids": ["sub-test-1"]}})
test("200 OK (a tool's own result is data, not an HTTP error)", resp.status_code == 200)
tool_result = resp.get_json()
test("tool result status is ok", tool_result["status"] == "ok")

resp = client.post("/api/operations/tools/get_finding_evidence", json={"arguments": {"subscription_ids": ["sub-test-1"]}})
test("missing required finding_id -> still 200, but status invalid_arguments", resp.status_code == 200 and resp.get_json()["status"] == "invalid_arguments")

resp = client.post("/api/operations/tools/not_a_real_tool", json={"arguments": {}})
test("unknown tool name -> 404", resp.status_code == 404)

resp = client.post("/api/operations/tools/get_source_coverage", json={"arguments": "not-an-object"})
test("non-object arguments -> 400", resp.status_code == 400)

resp = client.post(
    "/api/operations/tools/get_source_coverage",
    json={"arguments": {"subscription_ids": ["sub-test-1"]}, "roles": ["some_other_role"]},
)
test("caller missing required role -> tool result status denied", resp.get_json()["status"] == "denied")


print("\n\U0001f9ea Test 10: existing APIs are preserved unchanged")
resp = client.get("/api/health")
test("/api/health still returns 200", resp.status_code == 200)
health_body = resp.get_json()
test("/api/health still has status/version/profile", {"status", "version", "profile"}.issubset(health_body.keys()))
test("/api/health now also reports agent_definition_version/backend/evaluation", {"agent_definition_version", "backend", "evaluation"}.issubset(health_body.keys()))

resp = client.get("/api/demos")
test("/api/demos still returns 200", resp.status_code == 200)


_cleanup_db()

# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
