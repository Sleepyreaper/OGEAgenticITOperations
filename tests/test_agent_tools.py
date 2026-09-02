#!/usr/bin/env python3
"""Test the typed, read-only tool registry (app/agents/tools.py) --
JSON schema validation, authorization, read-only classification, result
bounds, and timeout enforcement. get_snapshot is monkeypatched to a
canned fake -- no real Azure calls.

Run: python3 tests/test_agent_tools.py
"""
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-test-1"

from app.agents import tools as tools_mod  # noqa: E402
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


FAKE_SNAPSHOT = OperationsSnapshot(
    id="snap-1", generated_at="2025-06-01T00:00:00.000Z", subscription_ids=("sub-test-1",), status="ok",
    envelopes=[], findings=[], coverage={"total_sources": 5, "ok_count": 5}, source_errors=[],
    summary={"total_findings": 0},
)
tools_mod.get_snapshot = lambda subs, config=None, force_refresh=False: FAKE_SNAPSHOT


print("\n\U0001f9ea Test 1: registry contents -- every tool is read-only, has a schema, a role, bounds")
definitions = tools_mod.list_tool_definitions()
test("registry has exactly the 6 documented tools", len(definitions) == 6)
test("every tool is read_only", all(d["read_only"] is True for d in definitions))
test("every tool has a non-empty required_role", all(d["required_role"] for d in definitions))
test("every tool has a positive timeout_seconds", all(d["timeout_seconds"] > 0 for d in definitions))
test("every tool has a positive max_result_items bound", all(d["max_result_items"] > 0 for d in definitions))
test("every tool's parameters_schema requires subscription_ids", all("subscription_ids" in d["parameters_schema"]["required"] for d in definitions))
test("no tool schema allows additionalProperties", all(d["parameters_schema"]["additionalProperties"] is False for d in definitions))


print("\n\U0001f9ea Test 2: execute_tool -- happy path returns a bounded ok ToolResult")
result1 = tools_mod.execute_tool("get_source_coverage", {"subscription_ids": ["sub-test-1"]})
test("status is ok", result1.status == "ok")
test("result_count reflects the handler's own count", result1.result_count == 5)
test("data is present", result1.data == {"coverage": {"total_sources": 5, "ok_count": 5}})
test("duration_ms is recorded", result1.duration_ms >= 0)


print("\n\U0001f9ea Test 3: execute_tool -- unknown tool name")
result2 = tools_mod.execute_tool("delete_everything", {"subscription_ids": ["sub-test-1"]})
test("unknown tool -> status error, never silently a no-op", result2.status == "error")


print("\n\U0001f9ea Test 4: execute_tool -- JSON Schema validation rejects bad arguments")
result3 = tools_mod.execute_tool("get_finding_evidence", {"subscription_ids": ["sub-test-1"]})
test("missing required finding_id -> invalid_arguments", result3.status == "invalid_arguments")

result4 = tools_mod.execute_tool("get_source_coverage", {"subscription_ids": ["sub-test-1"], "not_a_real_field": 1})
test("unexpected extra property -> invalid_arguments", result4.status == "invalid_arguments")

result5 = tools_mod.execute_tool("get_source_coverage", {"subscription_ids": []})
test("empty subscription_ids violates minItems -> invalid_arguments", result5.status == "invalid_arguments")

result6 = tools_mod.execute_tool("list_prioritized_findings", {"subscription_ids": ["sub-test-1"], "page_size": 9999})
test("page_size above the schema's maximum -> invalid_arguments", result6.status == "invalid_arguments")


print("\n\U0001f9ea Test 5: execute_tool -- authorization (required_role)")
result7 = tools_mod.execute_tool("get_source_coverage", {"subscription_ids": ["sub-test-1"]}, caller_roles={"some_other_role"})
test("caller missing the required role -> denied", result7.status == "denied")

result8 = tools_mod.execute_tool("get_source_coverage", {"subscription_ids": ["sub-test-1"]}, caller_roles={"operations_reader"})
test("caller with the required role -> ok", result8.status == "ok")

result9 = tools_mod.execute_tool("get_source_coverage", {"subscription_ids": ["sub-test-1"]}, caller_roles=None)
test("caller_roles=None (no authorization context) -> allowed (documented default-trust state)", result9.status == "ok")


print("\n\U0001f9ea Test 6: execute_tool -- wall-clock timeout enforcement")
original_tool = tools_mod.TOOLS["get_source_coverage"]


def _slow_handler(arguments, config):
    time.sleep(0.3)
    return {"items": list(range(50))}, 50


tools_mod.TOOLS["get_source_coverage"] = tools_mod.ToolDefinition(
    name="get_source_coverage", description="d", parameters_schema=original_tool.parameters_schema,
    handler=_slow_handler, timeout_seconds=0.05, max_result_items=10,
)
result10 = tools_mod.execute_tool("get_source_coverage", {"subscription_ids": ["sub-test-1"]})
test("a handler exceeding timeout_seconds -> status timeout", result10.status == "timeout")
test("timeout result has no data", result10.data is None)


print("\n\U0001f9ea Test 7: execute_tool -- result-size bound is enforced even if the handler forgets")
def _oversized_handler(arguments, config):
    return {"items": list(range(100))}, 100


tools_mod.TOOLS["get_source_coverage"] = tools_mod.ToolDefinition(
    name="get_source_coverage", description="d", parameters_schema=original_tool.parameters_schema,
    handler=_oversized_handler, timeout_seconds=5.0, max_result_items=10,
)
result11 = tools_mod.execute_tool("get_source_coverage", {"subscription_ids": ["sub-test-1"]})
test("data list is truncated to max_result_items regardless of what the handler returned", len(result11.data["items"]) == 10)
test("result_count still reflects the handler's own reported count (100), not the truncated length", result11.result_count == 100)

tools_mod.TOOLS["get_source_coverage"] = original_tool


print("\n\U0001f9ea Test 8: execute_tool -- an unexpected handler exception becomes an explicit error, never a crash")
def _broken_handler(arguments, config):
    raise RuntimeError("boom")


tools_mod.TOOLS["get_source_coverage"] = tools_mod.ToolDefinition(
    name="get_source_coverage", description="d", parameters_schema=original_tool.parameters_schema,
    handler=_broken_handler, timeout_seconds=5.0, max_result_items=10,
)
result12 = tools_mod.execute_tool("get_source_coverage", {"subscription_ids": ["sub-test-1"]})
test("an unexpected handler exception -> status error with the message surfaced", result12.status == "error" and "boom" in result12.error)

tools_mod.TOOLS["get_source_coverage"] = original_tool


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
